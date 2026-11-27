"""VPN 등록부. 모든 VPN 은 여기에 등록된 것뿐이다. gui/server.py(웹 화면)와 gui/vpnctl.py(명령줄)가 쓴다.

  vpns.json                등록한 VPN 의 이름·포트·폴더(git 제외, 0600)
  compose.generated.yml    등록부에서 만든 compose 서비스(git 제외, 0600). 손으로 고치지 않는다.
  ovpn/<이름>/<이름>.ovpn   올린 ovpn(0600, 폴더 0700).  ovpn/<이름>/auth.txt  아이디·비밀번호(0600, 필요할 때만)

  등록하는 길은 둘이다: 웹에서 올리기(add), 이미 있는 ovpn/<이름>/ 폴더 가져오기(import_folder).
  가져온 폴더는 사용자가 직접 넣은 것이므로 화면에서 지워도 폴더는 남기고, 화면에서 교체하지 않는다.

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
PORT_FIRST, PORT_LAST = 1081, 1180  # 자동으로 고르는 범위
PORT_MIN, PORT_MAX = 1024, 65535  # 직접 정할 때 받는 범위
DNS_MAX = 4
UPLOAD_TMP = ".upload.tmp"  # 교체할 때 잠깐 쓰는 이름(.ovpn 으로 끝나지 않는다)
MAX_VPNS = 50
CHECK_TIMEOUT = 15
REASON_MAX, REASON_LINE_MAX = 20, 300

# docker-compose.yml 의 vpn-template 서비스와 같은 값이어야 한다(tests/gui_test.py 가 비교한다).
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
_IPV4 = re.compile(r"(?:(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])")


def dns_list(value):
    """DNS 주소 목록(IPv4, 127.x·0.x 제외, 최대 DNS_MAX 개). 모양이 다르면 None. 글자열이면 쉼표로 나눈다."""
    if value in (None, "", []):
        return []
    items = [x.strip() for x in value.split(",")] if isinstance(value, str) else value
    if not isinstance(items, list) or not 1 <= len(items) <= DNS_MAX:
        return None
    for x in items:
        if not isinstance(x, str) or not _IPV4.fullmatch(x) or x.startswith(("127.", "0.")):
            return None
    return items


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
        """등록된 VPN [{name, port, dns, imported}]. 파일이 없거나 깨졌으면 모양이 맞는 것만."""
        try:
            data = json.loads((self.root / REGISTRY).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out, seen, ports = [], set(), set()
        rows = data.get("vpns") if isinstance(data, dict) else None
        for e in rows if isinstance(rows, list) else []:
            if not isinstance(e, dict):
                continue
            name, port = e.get("name"), e.get("port")
            if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in seen:
                continue
            if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX or port in ports:
                continue
            seen.add(name)
            ports.add(port)
            out.append({"name": name, "port": port, "dns": dns_list(e.get("dns")) or [], "imported": e.get("imported") is True})
        return out[:MAX_VPNS]

    def names(self):
        return {e["name"] for e in self.entries()}

    def generated_path(self):
        return self.root / GENERATED

    def compose_files(self):
        """docker compose 에 넘길 -f 인자. 언제나 기본 파일과 생성 파일 둘이다(생성 파일이 없으면 빈 것을 만든다)."""
        if not self.generated_path().is_file():
            self.save(self.entries())
        return ["-f", BASE_COMPOSE, "-f", GENERATED]

    @staticmethod
    def service(entry):
        name = entry["name"]
        env = {"VPN_NAME": name, "VPN_CONNECT_TIMEOUT": "${VPN_CONNECT_TIMEOUT:-60}"}
        if entry.get("dns"):
            env["VPN_DNS"] = ",".join(entry["dns"])
        return {
            **SERVICE_BASE,
            "container_name": CONTAINER_PREFIX + name,
            "environment": env,
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
        rows = []
        for e in entries:
            row = {"name": e["name"], "port": e["port"], "folder": f"{OVPN_DIR}/{e['name']}"}
            if e.get("dns"):
                row["dns"] = e["dns"]
            if e.get("imported"):
                row["imported"] = True
            rows.append(row)
        self._write(REGISTRY, json.dumps({"vpns": rows}, ensure_ascii=False, indent=2) + "\n")
        self._write(GENERATED, json.dumps({"services": {e["name"]: self.service(e) for e in entries}}, indent=2) + "\n")

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

    @staticmethod
    def _upload(ovpn, username, password):
        """올린 값의 모양 검사. (ovpn 바이트, 아이디·비밀번호가 필요한가, 아이디, 비밀번호)."""
        if not isinstance(ovpn, str) or not ovpn.strip():
            raise Rejected("ovpn 파일이 비어 있습니다.")
        try:
            data = ovpn.encode("ascii")
        except UnicodeEncodeError:
            raise Rejected("ovpn 에 받지 않는 글자가 있습니다(인쇄 가능한 ASCII·탭만 받습니다. 한글 주석 등은 지우세요).") from None
        if len(data) > OVPN_MAX:
            raise Rejected(f"ovpn 파일이 너무 큽니다({OVPN_MAX // 1024}KB 까지).")
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
        return data, needs_auth, username, password

    def _free_port(self, entries, taken_ports=()):
        used = set(taken_ports) | {e["port"] for e in entries}
        port = next((p for p in range(PORT_FIRST, PORT_LAST + 1) if p not in used and self.port_free(p)), None)
        if port is None:
            raise Rejected(f"빈 포트가 없습니다({PORT_FIRST}~{PORT_LAST}).")
        return port

    # ── 추가·교체·삭제 ─────────────────────────────────────────────────────
    def add(self, name, ovpn, username="", password="", taken_ids=(), taken_containers=(), taken_ports=(), clean=lambda s: s):
        """올린 ovpn 을 저장하고 등록한다. 돌려주는 값은 {name, port, auth}. 문제가 있으면 Rejected."""
        folder = self._folder(name)
        data, needs_auth, username, password = self._upload(ovpn, username, password)
        entries = self.entries()
        if len(entries) >= MAX_VPNS:
            raise Rejected(f"등록할 수 있는 VPN 은 {MAX_VPNS}개까지입니다.")
        if name in taken_ids or name in {e["name"] for e in entries} or CONTAINER_PREFIX + name in taken_containers:
            raise Rejected("이미 있는 이름입니다. 다른 이름을 쓰거나 '같은 이름이면 교체'를 고르세요.")
        if folder.exists() or folder.is_symlink():
            raise Rejected(f"{OVPN_DIR}/{name} 폴더가 이미 있습니다. 다른 이름을 쓰세요(직접 넣은 폴더는 gui/vpnctl.py import 로 등록합니다).")
        port = self._free_port(entries, taken_ports)

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

    def replace(self, name, ovpn, username="", password="", clean=lambda s: s):
        """웹에서 올린 VPN 의 ovpn 을 새 것으로 바꾼다. 포트와 이름은 그대로다. 새 것이 검사를 통과한 뒤에만 바꾼다."""
        folder = self._folder(name)
        entry = next((e for e in self.entries() if e["name"] == name), None)
        if entry is None:
            raise Rejected("등록되지 않은 이름입니다.")
        if entry["imported"]:
            raise Rejected("직접 넣은 폴더(가져온 VPN)는 화면에서 교체하지 않습니다. 폴더의 파일을 직접 바꾸세요.")
        if folder.is_symlink() or not folder.is_dir():
            raise Rejected(f"{OVPN_DIR}/{name} 폴더가 없습니다.")
        data, needs_auth, username, password = self._upload(ovpn, username, password)
        tmp = folder / UPLOAD_TMP
        try:
            if tmp.is_symlink() or tmp.exists():
                os.unlink(tmp)
            self._put(tmp, data)
            reasons = self.check(tmp, clean)
            if reasons:
                raise Rejected("ovpn 검사를 통과하지 못했습니다. 기존 것은 그대로 두었습니다.", reasons)
            for f in list(os.scandir(folder)):  # 예전 ovpn·아이디 파일을 지운다(이 폴더는 올린 파일만 들어 있다)
                if f.name != UPLOAD_TMP and (f.name.endswith(".ovpn") or f.name == AUTH_FILE) and not f.is_dir(follow_symlinks=False):
                    os.unlink(f.path)
            os.replace(tmp, folder / (name + ".ovpn"))
            if needs_auth:
                self._put(folder / AUTH_FILE, (username + "\n" + password + "\n").encode("ascii"))
        finally:
            if tmp.is_symlink() or tmp.exists():
                os.unlink(tmp)
        return {"name": name, "port": entry["port"], "auth": needs_auth}

    def import_folder(self, name, port=None, dns=None, clean=lambda s: s):
        """이미 있는 ovpn/<이름>/ 폴더를 등록한다(직접 넣은 것, 예전 방식에서 옮길 때). 포트를 정할 수 있다.

        폴더에는 .ovpn 이 1개 있어야 하고 검사를 통과해야 한다. 폴더를 0700, 안의 파일을 0600 으로 좁힌다.
        """
        folder = self._folder(name)
        entries = self.entries()
        if name in {e["name"] for e in entries}:
            raise Rejected("이미 등록된 이름입니다.")
        if len(entries) >= MAX_VPNS:
            raise Rejected(f"등록할 수 있는 VPN 은 {MAX_VPNS}개까지입니다.")
        if folder.is_symlink() or not folder.is_dir():
            raise Rejected(f"{OVPN_DIR}/{name} 폴더가 없습니다(링크는 받지 않습니다).")
        files = [f for f in os.scandir(folder) if f.is_file(follow_symlinks=False)]
        ovpns = [f for f in files if f.name.endswith(".ovpn")]
        if len(ovpns) != 1:
            raise Rejected(f"{OVPN_DIR}/{name} 폴더에 .ovpn 파일이 {len(ovpns)}개 있습니다. 1개만 두세요.")
        if ovpns[0].stat(follow_symlinks=False).st_size > OVPN_MAX:
            raise Rejected(f"ovpn 파일이 너무 큽니다({OVPN_MAX // 1024}KB 까지).")
        dns = dns_list(dns)
        if dns is None:
            raise Rejected(f"DNS 는 IPv4 주소 {DNS_MAX}개까지입니다(127.x·0.x 불가).")
        if port is None:
            port = self._free_port(entries)
        elif isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
            raise Rejected(f"포트는 {PORT_MIN}~{PORT_MAX} 의 숫자여야 합니다.")
        elif port in {e["port"] for e in entries}:
            raise Rejected("다른 VPN 이 쓰는 포트입니다.")
        reasons = self.check(ovpns[0].path, clean)
        if reasons:
            raise Rejected("ovpn 검사를 통과하지 못했습니다.", reasons)
        os.chmod(folder, 0o700)
        for f in files:
            os.chmod(f.path, 0o600)
        self.save(entries + [{"name": name, "port": port, "dns": dns, "imported": True}])
        return {"name": name, "port": port, "dns": dns}

    def update(self, name, port=None, dns=None):
        """등록된 VPN 의 포트·DNS 를 바꾼다(None 이면 그대로). 컨테이너를 다시 띄워야 적용된다."""
        entries = self.entries()
        entry = next((e for e in entries if e["name"] == name), None)
        if entry is None:
            raise Rejected("등록되지 않은 이름입니다.")
        if port is not None:
            if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
                raise Rejected(f"포트는 {PORT_MIN}~{PORT_MAX} 의 숫자여야 합니다.")
            if port in {e["port"] for e in entries if e is not entry}:
                raise Rejected("다른 VPN 이 쓰는 포트입니다.")
            entry["port"] = port
        if dns is not None:
            parsed = dns_list(dns)
            if parsed is None:
                raise Rejected(f"DNS 는 IPv4 주소 {DNS_MAX}개까지입니다(127.x·0.x 불가).")
            entry["dns"] = parsed
        self.save(entries)
        return {"name": name, "port": entry["port"], "dns": entry["dns"]}

    def forget(self, name):
        """등록부에서만 뺀다(폴더는 그대로). 뺐으면 True."""
        entries = self.entries()
        rest = [e for e in entries if e["name"] != name]
        if len(rest) == len(entries):
            return False
        self.save(rest)
        return True

    def remove(self, name):
        """등록부에서 빼고 ovpn/<이름> 폴더를 지운다. 가져온 폴더(직접 넣은 것)는 남긴다. 폴더가 없어졌으면 True."""
        folder = self._folder(name)
        entry = next((e for e in self.entries() if e["name"] == name), None)
        if entry is None:
            raise Rejected("등록된 VPN 만 지울 수 있습니다.")
        self.forget(name)
        if entry["imported"]:
            return False
        if folder.is_symlink():
            os.unlink(folder)  # 링크가 가리키는 곳은 건드리지 않는다
        elif folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()
