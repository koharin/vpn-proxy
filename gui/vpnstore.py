"""웹 화면에서 올린 VPN 의 등록부. gui/server.py 가 쓴다.

  vpns.json                등록한 VPN 의 이름·포트·폴더(git 제외, 0600)
  compose.generated.yml    등록부에서 만든 compose 서비스(git 제외, 0600). 손으로 고치지 않는다.
  ovpn/<이름>/<이름>.ovpn   올린 ovpn(0600, 폴더 0700).  ovpn/<이름>/auth.txt  아이디·비밀번호(0600, 필요할 때만)

지키는 것
  - 이름은 소문자·숫자·'-' 뿐이고, 폴더는 언제나 ovpn/<이름> 이다(요청 값으로 경로를 만들지 않는다).
  - ovpn 은 저장한 뒤 rootfs/check-ovpn.sh(컨테이너가 시작할 때 쓰는 그 검사)로 검사한다.
    통과하지 못하면 폴더째 지우고 사유(줄 번호)만 돌려준다.
  - 올린 것은 파일 하나뿐이므로 인증서·키는 ovpn 안의 <ca>·<cert>·<key> 블록이어야 한다.
  - 이 파일은 저장만 한다. ovpn·auth 파일을 다시 읽어 돌려주는 코드는 없다. 비밀번호는 응답·로그에 넣지 않는다.
  - 포트는 127.0.0.1 로만 연다.
"""
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

REGISTRY = "vpns.json"
GENERATED = "compose.generated.yml"
BASE_COMPOSE = "docker-compose.yml"
OVPN_DIR = "ovpn"
AUTH_FILE = "auth.txt"
CONTAINER_PREFIX = "vpn-proxy-"
# 검사는 fullmatch 로만 한다('$' 는 끝의 줄바꿈 앞에서도 맞는다).
NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?")
OVPN_MAX = 256 * 1024
CRED_MAX = 256
PORT_FIRST, PORT_LAST = 1081, 1180
MAX_VPNS = 50
CHECK_TIMEOUT = 15
REASON_MAX, REASON_LINE_MAX = 20, 300

# docker-compose.yml 의 x-vpn 블록과 같은 값이어야 한다(tests/gui_test.py 가 비교한다).
SERVICE_BASE = {
    "build": ".",
    "image": "vpn-proxy:local",
    "restart": "unless-stopped",
    "cap_drop": ["ALL"],
    "cap_add": ["NET_ADMIN", "SETUID", "SETGID"],
    "devices": ["/dev/net/tun"],
    "security_opt": ["no-new-privileges:true"],
    "sysctls": {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv4.ping_group_range": "0 2147483647"},
    "tmpfs": ["/run", "/tmp"],
    "stop_grace_period": "5s",
}

# 첫 인자가 파일 경로인 지시(check-ovpn.sh 의 path_arg 와 같다).
_PATH_ARG = frozenset("ca cert key extra-certs pkcs12 dh crl-verify tls-auth tls-crypt tls-crypt-v2 auth-user-pass".split())
_BLOCK = re.compile(r"^<(/?)([a-z0-9-]+)>")
_CRED_OK = re.compile(r"[\x20-\x7e]{1,%d}" % CRED_MAX)
_CHECK_LINE = re.compile(r"^\s{0,4}\S")


class Rejected(Exception):
    """받지 않는 요청. message 는 화면에 그대로 내도 되는 글이다(올린 내용·비밀번호를 담지 않는다)."""

    def __init__(self, message, reasons=None):
        super().__init__(message)
        self.message = message
        self.reasons = list(reasons or [])


def port_free(port):
    """127.0.0.1 의 그 포트를 지금 열 수 있는가(다른 프로그램이 쓰고 있지 않은가)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def scan(text):
    """ovpn 글에서 (auth 파일이 필요한가, [(줄 번호, 지시)] 바깥 파일을 가리키는 줄).

    블록(<ca> 등) 안쪽은 보지 않는다. <connection> 안쪽은 지시로 본다(check-ovpn.sh 와 같다).
    """
    needs_auth, inline_auth, outside, skip = False, False, [], ""
    for no, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r").strip(" \t")
        if skip:
            if line.startswith("</" + skip + ">"):
                skip = ""
            continue
        if not line or line[0] in "#;":
            continue
        m = _BLOCK.match(line)
        if m:
            if m.group(2) != "connection" and not m.group(1):
                skip = m.group(2)
                inline_auth = inline_auth or skip == "auth-user-pass"
            continue
        words = line.split()
        while words[:2] == ["setenv", "opt"]:
            words = words[2:]
        if not words or words[0] not in _PATH_ARG:
            continue
        if words[0] == "auth-user-pass" and len(words) == 1:
            needs_auth = True
        elif len(words) > 1 and not (words[0] == "dh" and words[1] == "none"):
            outside.append((no, words[0]))
    return needs_auth and not inline_auth, outside


class Store:
    def __init__(self, root, port_free=port_free):
        self.root = Path(root)
        self.port_free = port_free

    # ── 등록부 ──────────────────────────────────────────────────────────────
    def entries(self):
        """등록된 VPN [{name, port}] . 파일이 없거나 깨졌으면 모양이 맞는 것만."""
        try:
            data = json.loads((self.root / REGISTRY).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out, seen = [], set()
        rows = data.get("vpns") if isinstance(data, dict) else None
        for e in rows if isinstance(rows, list) else []:
            if not isinstance(e, dict):
                continue
            name, port = e.get("name"), e.get("port")
            if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in seen:
                continue
            if isinstance(port, bool) or not isinstance(port, int) or not PORT_FIRST <= port <= PORT_LAST:
                continue
            seen.add(name)
            out.append({"name": name, "port": port})
        return out[:MAX_VPNS]

    def names(self):
        return {e["name"] for e in self.entries()}

    def generated_path(self):
        return self.root / GENERATED

    def compose_files(self):
        """docker compose 에 넘길 -f 인자. 올린 VPN 이 없으면 빈 목록(기본 파일만 쓴다)."""
        return ["-f", BASE_COMPOSE, "-f", GENERATED] if self.generated_path().is_file() else []

    @staticmethod
    def service(entry):
        name = entry["name"]
        return {
            **SERVICE_BASE,
            "container_name": CONTAINER_PREFIX + name,
            "environment": {"VPN_NAME": name, "VPN_CONNECT_TIMEOUT": "${VPN_CONNECT_TIMEOUT:-60}"},
            "volumes": [f"./{OVPN_DIR}/{name}:/vpn:ro"],
            "ports": [f"127.0.0.1:{entry['port']}:1080"],
        }

    def _write(self, name, text):
        """root 바로 아래에 0600 으로 바꿔 쓴다(임시 파일 → rename)."""
        tmp = self.root / (name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.root / name)

    def save(self, entries):
        """등록부와 생성 compose 를 함께 쓴다. compose 파일은 JSON 이다(YAML 은 JSON 을 그대로 읽는다)."""
        rows = [{"name": e["name"], "port": e["port"], "folder": f"{OVPN_DIR}/{e['name']}"} for e in entries]
        self._write(REGISTRY, json.dumps({"vpns": rows}, ensure_ascii=False, indent=2) + "\n")
        if entries:
            self._write(GENERATED, json.dumps({"services": {e["name"]: self.service(e) for e in entries}}, indent=2) + "\n")
        else:
            try:
                os.unlink(self.generated_path())
            except FileNotFoundError:
                pass

    # ── 검사 ────────────────────────────────────────────────────────────────
    def check(self, path, clean=lambda s: s):
        """rootfs/check-ovpn.sh 로 검사. 통과하면 [], 아니면 사유 줄들. 검사기를 못 돌리면 Rejected."""
        script = self.root / "rootfs" / "check-ovpn.sh"
        if not script.is_file():
            raise Rejected("ovpn 검사기(rootfs/check-ovpn.sh)가 없어 올릴 수 없습니다.")
        try:
            p = subprocess.run(["/bin/sh", str(script), str(path)], cwd=str(self.root), capture_output=True,
                               timeout=CHECK_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise Rejected("ovpn 검사기를 실행하지 못했습니다.") from None
        if p.returncode == 0:
            return []
        lines = [clean(l.strip())[:REASON_LINE_MAX] for l in p.stdout.decode("utf-8", "replace").splitlines() if _CHECK_LINE.match(l)]
        return lines[:REASON_MAX] or ["ovpn 검사를 통과하지 못했습니다."]

    def _folder(self, name):
        """ovpn/<이름>. 이름은 NAME_RE 를 통과한 것만 받는다. ovpn 폴더가 링크면 쓰지 않는다."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise Rejected("이름은 소문자·숫자·'-' 로 1~24자, 처음과 끝은 소문자나 숫자여야 합니다.")
        base = self.root / OVPN_DIR
        if base.is_symlink() or not base.is_dir():
            raise Rejected("ovpn 폴더가 없습니다(또는 링크입니다).")
        return base / name

    @staticmethod
    def _put(path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, 0o600)

    # ── 추가·삭제 ───────────────────────────────────────────────────────────
    def add(self, name, ovpn, username="", password="", taken_ids=(), taken_containers=(), taken_ports=(), clean=lambda s: s):
        """올린 ovpn 을 저장하고 등록한다. 돌려주는 값은 {name, port, auth}. 문제가 있으면 Rejected."""
        folder = self._folder(name)
        if not isinstance(ovpn, str) or not ovpn.strip():
            raise Rejected("ovpn 파일이 비어 있습니다.")
        try:
            data = ovpn.encode("ascii")
        except UnicodeEncodeError:
            raise Rejected("ovpn 에 받지 않는 글자가 있습니다(인쇄 가능한 ASCII·탭만 받습니다. 한글 주석 등은 지우세요).") from None
        if len(data) > OVPN_MAX:
            raise Rejected(f"ovpn 파일이 너무 큽니다({OVPN_MAX // 1024}KB 까지).")
        entries = self.entries()
        if len(entries) >= MAX_VPNS:
            raise Rejected(f"올릴 수 있는 VPN 은 {MAX_VPNS}개까지입니다.")
        if name in taken_ids or name in {e["name"] for e in entries} or CONTAINER_PREFIX + name in taken_containers:
            raise Rejected("이미 있는 이름입니다. 다른 이름을 쓰세요.")
        if folder.exists() or folder.is_symlink():
            raise Rejected(f"{OVPN_DIR}/{name} 폴더가 이미 있습니다. 다른 이름을 쓰세요.")

        needs_auth, outside = scan(ovpn)
        if outside:
            raise Rejected(
                "다른 파일을 가리키는 줄이 있습니다. 올릴 수 있는 것은 ovpn 파일 하나뿐이니 인증서·키는 ovpn 안에 "
                "<ca>·<cert>·<key> 블록으로 넣고, 아이디·비밀번호는 파일 이름 없이 auth-user-pass 만 적으세요.",
                [f"{no}번째 줄: \"{word}\" 가 파일을 가리킵니다" for no, word in outside[:REASON_MAX]],
            )
        username, password = username or "", password or ""
        if needs_auth:
            if not username or not password:
                raise Rejected("이 ovpn 은 아이디·비밀번호가 필요합니다(auth-user-pass). 두 칸을 채우세요.")
            if not isinstance(username, str) or not isinstance(password, str) or not _CRED_OK.fullmatch(username) or not _CRED_OK.fullmatch(password):
                raise Rejected(f"아이디·비밀번호는 인쇄 가능한 ASCII 로 {CRED_MAX}자까지입니다(줄바꿈 불가).")

        used = set(taken_ports) | {e["port"] for e in entries}
        port = next((p for p in range(PORT_FIRST, PORT_LAST + 1) if p not in used and self.port_free(p)), None)
        if port is None:
            raise Rejected(f"빈 포트가 없습니다({PORT_FIRST}~{PORT_LAST}).")

        try:
            os.mkdir(folder, 0o700)
        except OSError:
            raise Rejected(f"{OVPN_DIR}/{name} 폴더를 만들지 못했습니다.") from None
        try:
            os.chmod(folder, 0o700)
            target = folder / (name + ".ovpn")
            self._put(target, data)
            reasons = self.check(target, clean)
            if reasons:
                raise Rejected("ovpn 검사를 통과하지 못했습니다. 아래 줄을 고친 뒤 다시 올리세요.", reasons)
            if needs_auth:
                self._put(folder / AUTH_FILE, (username + "\n" + password + "\n").encode("ascii"))
            entry = {"name": name, "port": port}
            self.save(entries + [entry])
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return {"name": name, "port": port, "auth": needs_auth}

    def forget(self, name):
        """등록부에서만 뺀다(폴더는 그대로). 뺐으면 True."""
        entries = self.entries()
        rest = [e for e in entries if e["name"] != name]
        if len(rest) == len(entries):
            return False
        self.save(rest)
        return True

    def remove(self, name):
        """등록부에서 빼고 ovpn/<이름> 폴더를 지운다. 등록부에 있는 이름만 받는다."""
        folder = self._folder(name)
        if name not in self.names():
            raise Rejected("웹에서 올린 VPN 만 지울 수 있습니다.")
        self.forget(name)
        if folder.is_symlink():
            os.unlink(folder)  # 링크가 가리키는 곳은 건드리지 않는다
        elif folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()
