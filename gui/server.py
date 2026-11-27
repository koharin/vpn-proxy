#!/usr/bin/env python3
"""vpn-proxy 웹 화면. Mac(호스트)에서 돌고, 이 저장소의 docker compose 를 대신 부른다.

  python3 gui/server.py            → http://127.0.0.1:8030
  VPN_GUI_PORT=8031 python3 gui/server.py

하는 일
  - compose 에 정의된 VPN(프로필) 목록, 상태, SOCKS5 포트, 최근 로그를 보여 준다.
  - .ovpn 파일을 올리면 새 VPN 으로 등록한다(이름·포트·폴더는 vpns.json, 서비스는 compose.generated.yml).
    올린 VPN 은 화면에서 지울 수 있다. docker-compose.yml 에 적힌 VPN 은 지우지 않는다.
  - 프로필 제목은 그 폴더의 .ovpn 파일 이름, 부제는 그 ovpn 이 접속하는 서버(remote 호스트:포트/프로토콜)다.
  - 켜기 = docker compose up -d --no-build <서비스>,  끄기 = docker compose stop <서비스>.

지키는 것
  - 127.0.0.1 에만 연다(주소는 바꿀 수 없다. 포트만 바꾼다).
  - Host 머리말이 127.0.0.1/localhost 가 아니면 거절한다(다른 이름으로 이 주소를 가리키게 하는 공격 방지).
  - 상태를 바꾸는 요청은 POST 뿐이고, Origin 이 이 화면 자신일 때만 받는다. GET 은 아무것도 바꾸지 않는다.
  - 다룰 수 있는 대상은 compose 에 정의된 서비스 이름뿐이다. 요청 값은 그 목록과 비교만 한다.
  - 올린 ovpn 은 저장한 뒤 check-ovpn.sh(컨테이너가 시작할 때 도는 그 검사)로 검사하고, 통과한 것만 등록한다.
    저장·검사·삭제는 gui/vpnstore.py 가 한다. 올린 내용과 비밀번호는 응답·로그에 다시 내보내지 않는다.
  - ovpn 에서는 파일 이름과 remote·port·proto 줄만 읽는다. 인증서·키 블록과 그 밖의 줄, 아이디·비밀번호 파일은
    읽어 쓰지 않는다. 로그는 가려서 보여 준다.
  - 명령은 인자 목록으로만 실행한다(셸을 거치지 않는다).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import vpnstore

BIND_HOST = "127.0.0.1"  # 바꾸지 않는다
DEFAULT_PORT = 8030
ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
# 서비스·컨테이너 이름. 첫 글자는 영문·숫자여야 한다('-' 로 시작하면 docker 가 옵션으로 읽는다).
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
OVERVIEW_TTL = 1.5  # 목록·상태 결과를 이 시간(초) 동안 다시 쓴다
REQUEST_TIMEOUT = 10  # 연결 하나가 요청을 보내는 데 쓸 수 있는 시간(초)
LOG_LINES = 40
LOG_LINE_MAX = 300
MAX_BODY = 1024
UPLOAD_MAX_BODY = 2 * vpnstore.OVPN_MAX  # ovpn 을 JSON 글자열로 받는다(줄바꿈·따옴표가 2바이트가 된다)
CMD_TIMEOUT = 20
ACTION_TIMEOUT = 90

# 화면에 보이는 상태. 컨테이너 안 /run/vpn/state 의 단어를 이것으로 옮긴다.
OFF, CONNECTING, UP, FAILED, UNKNOWN = "off", "connecting", "up", "failed", "unknown"
INNER_TO_VIEW = {"starting": CONNECTING, "connecting": CONNECTING, "reconnecting": CONNECTING, "up": UP, "stopped": FAILED}

# 로그에서 가릴 것. 알려진 모양만 가린다(모든 비밀을 찾아내지는 못한다).
#   이름 뒤의 값: password=…, MY_PASSWORD: …, pwd …, private_key=…, "password is …" → 줄 끝까지
#   머리말: Authorization: …, Cookie: …, Set-Cookie: … → 줄 끝까지
#   주소 안의 비밀번호: scheme://이름:값@호스트
#   인증서·키: BEGIN~END 사이 줄 통째, 길게 이어진 base64 덩어리
_SECRET_NAMES = r"pass(?:word|wd|phrase)?|pwd|pw|secret|token|api[_-]?key|credentials?|cookie|private[ _-]?key|auth-token"
_SECRET_KV = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_-])*(?:" + _SECRET_NAMES + r")(?:[_-][A-Za-z0-9]+)*)(?![A-Za-z0-9_-])(\s*[:=]\s*|\s+is\s+|\s+)(\S.*)$"
)
_HEADER = re.compile(r"(?i)(?<![A-Za-z0-9-])((?:proxy-)?authorization|set-cookie|cookie)(\s*[:=]\s*)(\S.*)$")
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]*):[^\s/@]+@")
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]+-----")
_B64_ONLY = re.compile(r"^\s*[A-Za-z0-9+/=]{8,}\s*$")
_B64_LONG = re.compile(r"[A-Za-z0-9+/=]{48,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
HIDDEN = "[가림]"
LINE_SCAN_MAX = 2000  # 가리기 전에 줄을 이 길이로 자른다(아주 긴 줄에서 검사 시간이 길이의 제곱으로 늘지 않게)

# ovpn 에서 화면에 내는 것은 접속할 서버뿐이다. 값은 아래 모양일 때만 쓴다.
OVPN_READ_MAX = 256 * 1024
TITLE_MAX = 80
_REMOTE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
_REMOTE_PROTO = re.compile(r"^(?:udp|tcp)[46]?(?:-client)?$")
_PORT = re.compile(r"[0-9]{1,5}")
_BLOCK_OPEN = re.compile(r"^<([a-z0-9-]+)>$")


def _port(word):
    # isdigit() 은 '²'·'١' 같은 글자도 숫자로 보므로 쓰지 않는다(int() 가 못 읽거나 그대로 화면에 나간다).
    return word if _PORT.fullmatch(word) and 1 <= int(word) <= 65535 else None


def ovpn_target(path):
    """ovpn 이 접속하는 서버: ("호스트:포트/프로토콜", 그 밖의 remote 수). 못 읽으면 ("", 0).

    remote·port·proto 줄만 본다. <ca>·<key>·<tls-crypt> 같은 블록 안쪽은 건너뛴다.
    """
    try:
        if os.path.islink(path):
            return "", 0
        with open(path, "rb") as f:
            text = f.read(OVPN_READ_MAX).decode("utf-8", "replace")
    except OSError:
        return "", 0
    remotes, port, proto, skip = [], None, None, ""
    for raw in text.splitlines():
        line = raw.strip()
        if skip:
            if line == "</" + skip + ">":
                skip = ""
            continue
        m = _BLOCK_OPEN.match(line)
        if m:
            if m.group(1) != "connection":
                skip = m.group(1)
            continue
        words = line.split()
        if len(words) < 2:
            continue
        if words[0] == "remote" and _REMOTE_HOST.match(words[1]):
            remotes.append((words[1], _port(words[2]) if len(words) > 2 else None, words[3] if len(words) > 3 and _REMOTE_PROTO.match(words[3]) else None))
        elif words[0] == "port" and port is None:
            port = _port(words[1])
        elif words[0] == "proto" and proto is None and _REMOTE_PROTO.match(words[1]):
            proto = words[1]
    if not remotes:
        return "", 0
    host, rport, rproto = remotes[0]
    if ":" in host:
        host = "[" + host + "]"
    return f"{host}:{rport or port or '1194'}/{rproto or proto or 'udp'}", len(remotes) - 1


def redact_line(line):
    """로그 한 줄을 화면에 내도 되는 모양으로 바꾼다."""
    line = line[:LINE_SCAN_MAX]
    line = _ANSI.sub("", line)
    line = _CONTROL.sub("", line)
    line = _PEM_BEGIN.sub(HIDDEN, line)
    line = _PEM_END.sub(HIDDEN, line)
    line = _HEADER.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, line)
    line = _URL_USERINFO.sub(lambda m: m.group(1) + ":" + HIDDEN + "@", line)
    line = _SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + HIDDEN, line)
    line = _B64_LONG.sub(HIDDEN, line)
    if len(line) > LOG_LINE_MAX:
        line = line[:LOG_LINE_MAX] + "…"
    return line


def redact_log(text, limit=LOG_LINES):
    """로그 여러 줄. 인증서·키는 BEGIN~END 사이를 통째로 한 줄로 바꾼다."""
    out = []
    in_pem = False
    for raw in text.splitlines():
        raw = raw[:LINE_SCAN_MAX]
        if not raw.strip():
            continue
        if in_pem:
            if _PEM_END.search(raw):
                in_pem = False
            continue
        if _PEM_BEGIN.search(raw):
            out.append("[가림: 인증서·키 본문]")
            in_pem = not _PEM_END.search(raw)
            continue
        if _PEM_END.search(raw):
            # 시작 줄이 잘려 나간 본문: 바로 앞의 base64 줄들을 지운다
            while out and _B64_ONLY.match(out[-1].replace(HIDDEN, "AAAAAAAA")):
                out.pop()
            out.append("[가림: 인증서·키 본문]")
            continue
        out.append(redact_line(raw))
    return out[-limit:]


class Docker:
    """docker 명령을 부르는 한 곳. 인자는 목록으로만 넘긴다."""

    def __init__(self, binary=None, cwd=ROOT):
        self.binary = binary or os.environ.get("DOCKER", "docker")
        self.cwd = str(cwd)

    def run(self, args, timeout=CMD_TIMEOUT):
        try:
            p = subprocess.run([self.binary, *args], cwd=self.cwd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        except FileNotFoundError:
            return 127, "", "docker 명령을 찾지 못했습니다"
        except subprocess.TimeoutExpired:
            return 124, "", "시간 초과"


class Manager:
    def __init__(self, docker=None, root=ROOT):
        self.docker = docker or Docker(cwd=root)
        self.root = Path(root)
        self.store = vpnstore.Store(root)
        self._cache = None
        self._cache_at = 0.0
        self._lock = threading.Lock()  # 켜기·끄기는 한 번에 하나씩
        self._view = None
        self._view_at = 0.0
        self._view_lock = threading.Lock()  # 목록·상태 계산은 한 번에 하나씩

    # ── 프로필(compose 서비스) ──────────────────────────────────────────────
    def profiles(self, max_age=5.0):
        """compose 에 정의된 서비스 목록. 실패하면 (None, 이유)."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < max_age:
            return self._cache, None
        rc, out, _ = self.docker.run([*self._compose(), "config", "--format", "json"])
        if rc != 0:
            return None, "compose 설정을 읽지 못했습니다(docker compose config 실패). docker 가 설치돼 있는지, .env 값을 확인하세요."
        try:
            services = json.loads(out).get("services", {})
        except ValueError:
            return None, "compose 설정을 해석하지 못했습니다."
        result = []
        managed = self.store.names()
        for key in sorted(services):
            svc = services[key] or {}
            if not NAME_RE.match(key):
                continue
            container = svc.get("container_name") or ""
            if not NAME_RE.match(container):
                continue
            port, host_ip = None, None
            for p in svc.get("ports") or []:
                if isinstance(p, dict) and str(p.get("target")) == "1080":
                    port, host_ip = str(p.get("published") or ""), p.get("host_ip") or ""
            folder = None
            for v in svc.get("volumes") or []:
                if isinstance(v, dict) and v.get("target") == "/vpn":
                    folder = v.get("source")
            env = svc.get("environment") or {}
            label = env.get("VPN_NAME") if isinstance(env, dict) else None
            result.append(
                {
                    "id": key,
                    "name": label if isinstance(label, str) and NAME_RE.match(label) else key,
                    "container": container,
                    "socks_port": int(port) if port and port.isdigit() else None,
                    "socks_host": host_ip,
                    "folder": folder,
                    "managed": key in managed,  # 웹에서 올린 것(지울 수 있다)
                }
            )
        self._cache, self._cache_at = result, now
        return result, None

    def _compose(self):
        """compose 명령의 앞부분. 올린 VPN 이 있으면 생성 파일을 -f 로 함께 쓴다."""
        return ["compose", *self.store.compose_files()]

    def profile(self, profile_id):
        if not isinstance(profile_id, str) or not NAME_RE.match(profile_id):
            return None
        items, _ = self.profiles()
        for p in items or []:
            if p["id"] == profile_id:
                return p
        return None

    @staticmethod
    def _ovpn_names(folder):
        """폴더의 .ovpn 파일 이름들. 폴더가 없으면 None."""
        try:
            return sorted(f.name for f in os.scandir(folder) if f.is_file() and f.name.endswith(".ovpn"))
        except (OSError, TypeError):
            return None

    def _base_view(self, profile):
        """컨테이너 상태와 무관한 표시 값. .ovpn 이 1개일 때만 그 이름과 서버를 제목·부제로 쓴다."""
        names = self._ovpn_names(profile["folder"]) if profile["folder"] else None
        title, server, more = profile["name"], "", 0
        if names is not None and len(names) == 1:
            stem = _CONTROL.sub("", names[0][: -len(".ovpn")]).strip()[:TITLE_MAX]
            title = stem or title
            try:
                server, more = ovpn_target(os.path.join(profile["folder"], names[0]))
            except Exception:  # 깨진 ovpn 하나가 다른 프로필의 표시까지 막지 않게 한다
                server, more = "", 0
        return {
            "id": profile["id"],
            "name": profile["name"],
            "title": title,
            "server": server,
            "server_more": more,
            "socks_port": profile["socks_port"],
            "socks_host": profile["socks_host"],
            "managed": bool(profile.get("managed")),
            "folder": self._folder_label(profile["folder"]),
            "ovpn_files": len(names) if names is not None else None,
            "state": OFF,
            "message": "",
            "since": "",
            "dns": [],
            "restarts": 0,
            "log": [],
        }

    def _folder_label(self, folder):
        try:
            return str(Path(folder).resolve().relative_to(self.root.resolve()))
        except (ValueError, OSError, TypeError):
            return Path(folder).name if folder else ""

    # ── 상태 ────────────────────────────────────────────────────────────────
    def docker_ok(self):
        rc, _, _ = self.docker.run(["version", "--format", "{{.Server.Version}}"], timeout=8)
        return rc == 0

    def logs(self, profile):
        rc, out, err = self.docker.run(["logs", "--tail", "200", "--", profile["container"]])
        if rc != 0:
            return []
        return redact_log(out + err)

    @staticmethod
    def _last_error(lines):
        for line in reversed(lines):
            m = re.search(r"\] 오류: (.*)$", line)
            if m:
                return m.group(1)
        return ""

    def status(self, profile, with_logs=True):
        view = self._base_view(profile)
        rc, out, _ = self.docker.run(["inspect", "--format", "{{json .State}} {{.RestartCount}}", "--", profile["container"]])
        if rc != 0:
            return view  # 컨테이너가 없다 = 꺼짐
        try:
            state_json, restarts = out.strip().rsplit(" ", 1)
            st = json.loads(state_json)
            view["restarts"] = int(restarts)
        except ValueError:
            view["state"], view["message"] = UNKNOWN, "컨테이너 상태를 읽지 못했습니다."
            return view
        lines = self.logs(profile) if with_logs else []
        view["log"] = lines
        last_error = self._last_error(lines)
        if st.get("Restarting"):
            view["state"] = FAILED
            view["message"] = (last_error or "시작에 실패했습니다.") + " (자동으로 다시 시도합니다. 멈추려면 끄기)"
            return view
        if not st.get("Running"):
            return view  # 멈춰 있음 = 꺼짐
        rc, out, _ = self.docker.run(["exec", "--", profile["container"], "cat", "/run/vpn/status.json"])
        inner = {}
        if rc == 0:
            try:
                inner = json.loads(out)
            except ValueError:
                inner = {}
        word = inner.get("state") if isinstance(inner.get("state"), str) else ""
        view["state"] = INNER_TO_VIEW.get(word, CONNECTING)
        # 설정 확인에서 멈춘 경우(ovpn 없음 등)는 상태 파일을 만들기 전이라 읽을 것이 없다.
        # 이때 로그의 마지막 줄이 오류면 실패로 본다(컨테이너는 잠깐 쉬었다가 다시 시도한다).
        if not word and lines and re.search(r"\] 오류: ", lines[-1]):
            view["state"] = FAILED
            view["message"] = last_error
        view["message"] = redact_line(str(inner.get("message") or ""))
        since = inner.get("since")
        view["since"] = since if isinstance(since, str) and re.match(r"^[0-9T:Z-]{1,24}$", since) else ""
        dns = inner.get("dns")
        view["dns"] = [d for d in dns if isinstance(d, str) and re.match(r"^[0-9.]{7,15}$", d)] if isinstance(dns, list) else []
        if view["state"] == FAILED:
            view["message"] = (view["message"] or last_error or "실패") + " (자동으로 다시 시도합니다. 멈추려면 끄기)"
        elif view["state"] == CONNECTING and view["restarts"] > 0 and last_error:
            view["message"] = (view["message"] or "연결 중") + " — 직전 시도 실패: " + last_error
        return view

    def overview(self):
        """목록과 상태. 결과를 잠깐 다시 쓰고, 계산은 한 번에 하나만 한다(요청이 몰려도 docker 를 겹쳐 부르지 않는다)."""
        with self._view_lock:
            now = time.monotonic()
            if self._view is None or now - self._view_at >= OVERVIEW_TTL:
                self._view = self._overview()
                self._view_at = time.monotonic()
            return self._view

    def forget_overview(self):
        self._view = None

    def _overview(self):
        items, error = self.profiles()
        if items is None:
            return {"docker": False, "error": error, "profiles": []}
        if not self.docker_ok():
            return {
                "docker": False,
                "error": "Docker 가 꺼져 있습니다. Docker Desktop(또는 OrbStack)을 켠 뒤 다시 보세요.",
                "profiles": [dict(self.status_offline(p)) for p in items],
            }
        return {"docker": True, "error": "", "profiles": [self.status(p) for p in items]}

    def status_offline(self, profile):
        view = self._base_view(profile)
        view["state"], view["message"] = UNKNOWN, "Docker 가 꺼져 있어 상태를 알 수 없습니다."
        return view

    # ── 켜기·끄기 ──────────────────────────────────────────────────────────
    def switch(self, profile_id, on):
        """(성공 여부, 사람이 읽을 한 줄). 대상은 compose 목록에 있는 것만."""
        profile = self.profile(profile_id)
        if profile is None:
            return False, "그런 VPN 이 없습니다."
        if not self._lock.acquire(blocking=False):
            return False, "다른 켜기·끄기가 진행 중입니다. 잠시 뒤 다시 하세요."
        try:
            if on:
                args = [*self._compose(), "up", "-d", "--no-build", "--", profile["id"]]
            else:
                args = [*self._compose(), "stop", "--", profile["id"]]
            rc, _, err = self.docker.run(args, timeout=ACTION_TIMEOUT)
        finally:
            self._lock.release()
            self.forget_overview()
        if rc == 0:
            return True, ("켰습니다. 연결 상태는 아래에 표시됩니다." if on else "껐습니다.")
        low = err.lower()
        if "cannot connect" in low or "failed to connect" in low or "daemon" in low:
            return False, "Docker 가 꺼져 있습니다."
        if on and ("no such image" in low or "pull access denied" in low or "not found" in low):
            return False, "이미지가 없습니다. 먼저 터미널에서 docker compose build 를 실행하세요."
        if "port is already allocated" in low or "address already in use" in low:
            return False, "SOCKS5 포트를 다른 프로그램이 쓰고 있습니다. .env 의 포트를 바꾸세요."
        return False, "docker 명령이 실패했습니다. 터미널에서 docker compose logs 로 확인하세요."

    # ── 올리기·삭제 ────────────────────────────────────────────────────────
    def _forget_all(self):
        self._cache = None
        self.forget_overview()

    def add_vpn(self, body):
        """올린 ovpn 을 새 VPN 으로 등록한다. (성공 여부, 한 줄, 사유 줄들). 올린 내용은 돌려주지 않는다."""
        if not isinstance(body, dict) or any(not isinstance(body.get(k, ""), str) for k in ("name", "ovpn", "username", "password")):
            return False, "잘못된 요청입니다.", []
        if not self._lock.acquire(blocking=False):
            return False, "다른 작업이 진행 중입니다. 잠시 뒤 다시 하세요.", []
        try:
            items, error = self.profiles(max_age=0)
            if items is None:
                return False, error, []
            try:
                made = self.store.add(
                    body.get("name", ""), body.get("ovpn", ""), body.get("username", ""), body.get("password", ""),
                    taken_ids={p["id"] for p in items}, taken_containers={p["container"] for p in items},
                    taken_ports={p["socks_port"] for p in items if p["socks_port"]}, clean=redact_line,
                )
            except vpnstore.Rejected as e:
                return False, e.message, e.reasons
            except OSError:
                return False, "파일을 쓰지 못했습니다(폴더 권한을 확인하세요).", []
            # 생성한 compose 를 docker 가 받아들이는지 확인한다. 아니면 되돌린다.
            rc, _, _ = self.docker.run([*self._compose(), "config", "--quiet"])
            if rc != 0:
                try:
                    self.store.remove(made["name"])
                except (vpnstore.Rejected, OSError):
                    pass
                return False, "만든 compose 설정을 docker 가 받아들이지 않아 되돌렸습니다.", []
            note = " 아이디·비밀번호를 저장했습니다." if made["auth"] else ""
            return True, f"{made['name']} 을(를) 등록했습니다 — SOCKS5 127.0.0.1:{made['port']}.{note} 켜기를 누르면 연결합니다.", []
        finally:
            self._lock.release()
            self._forget_all()

    def delete_vpn(self, name, confirm):
        """올린 VPN 을 지운다: 컨테이너 정지·삭제 → 등록부에서 빼기 → 폴더 삭제. confirm 은 이름과 같아야 한다."""
        if not isinstance(name, str) or name not in self.store.names():
            return False, "웹에서 올린 VPN 만 지울 수 있습니다."
        if confirm != name:
            return False, "삭제 확인이 필요합니다."
        if not self._lock.acquire(blocking=False):
            return False, "다른 작업이 진행 중입니다. 잠시 뒤 다시 하세요."
        try:
            rc, _, _ = self.docker.run([*self._compose(), "rm", "--stop", "--force", "--", name], timeout=ACTION_TIMEOUT)
            if rc != 0:
                return False, "컨테이너를 멈추고 지우지 못해 그대로 두었습니다. Docker 가 켜져 있는지 확인하세요."
            try:
                gone = self.store.remove(name)
            except (vpnstore.Rejected, OSError):
                return False, "등록을 지우지 못했습니다."
            return True, "지웠습니다." if gone else "등록은 지웠지만 폴더가 남았습니다. ovpn/ 아래를 직접 확인하세요."
        finally:
            self._lock.release()
            self._forget_all()


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def make_handler(manager, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "vpn-proxy-gui"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT  # 요청을 반쯤 보내고 멈춘 연결을 끊는다

        def log_message(self, fmt, *args):  # 요청 값은 로그에 남기지 않는다
            pass

        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _fail(self, status, message):
            self._send(status, {"ok": False, "message": message})

        def _host_ok(self):
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._fail(403, "허용되지 않은 Host 입니다.")
            path = urlsplit(self.path).path
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                return self._send(200, (STATIC / name).read_bytes(), ctype)
            # 다른 웹사이트가 브라우저를 시켜 부른 요청은 받지 않는다(docker 를 반복 실행시키지 못하게).
            if path.startswith("/api/") and self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
                return self._fail(403, "허용되지 않은 출처입니다.")
            if path == "/api/profiles":
                return self._send(200, {"ok": True, **manager.overview()})
            if path == "/api/health":
                return self._send(200, {"ok": True})
            return self._fail(404, "없는 주소입니다.")

        do_HEAD = do_GET

        def do_POST(self):
            self.close_connection = True
            if not self._host_ok():
                return self._fail(403, "허용되지 않은 Host 입니다.")
            # 이 화면 자신이 보낸 요청만 받는다. Origin 이 없거나 다르면 거절한다.
            if self.headers.get("Origin", "") not in allowed_origins:
                return self._fail(403, "허용되지 않은 출처(Origin)입니다.")
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin",):
                return self._fail(403, "허용되지 않은 출처입니다.")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self._fail(415, "application/json 만 받습니다.")
            parts = urlsplit(self.path)
            if parts.query:
                return self._fail(400, "주소에 값을 붙일 수 없습니다.")
            m = re.match(r"^/api/profiles/([A-Za-z0-9][A-Za-z0-9._-]{0,31})/(on|off)$", parts.path)
            d = re.fullmatch(r"/api/vpns/([a-z0-9][a-z0-9-]{0,23})/delete", parts.path)
            upload = parts.path == "/api/vpns"
            if not (m or d or upload):
                return self._fail(404, "없는 주소입니다.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._fail(400, "잘못된 요청입니다.")
            if length < 0 or length > (UPLOAD_MAX_BODY if upload else MAX_BODY):
                return self._fail(413, "요청이 너무 큽니다." + (f" ovpn 은 {vpnstore.OVPN_MAX // 1024}KB 까지입니다." if upload else ""))
            raw = self.rfile.read(length) if length else b""
            if m:  # 본문 값은 쓰지 않는다
                ok, message = manager.switch(m.group(1), m.group(2) == "on")
                return self._send(200 if ok else 409, {"ok": ok, "message": message})
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, RecursionError):
                return self._fail(400, "잘못된 요청입니다.")
            if not isinstance(body, dict):
                return self._fail(400, "잘못된 요청입니다.")
            if d:
                ok, message = manager.delete_vpn(d.group(1), body.get("confirm"))
                return self._send(200 if ok else 409, {"ok": ok, "message": message})
            ok, message, reasons = manager.add_vpn(body)
            self._send(200 if ok else 409, {"ok": ok, "message": message, "reasons": reasons})

        def _not_allowed(self):
            self._fail(405, "지원하지 않는 방식입니다.")

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    return Handler


def make_server(manager=None, port=DEFAULT_PORT):
    manager = manager or Manager()
    server = ThreadingHTTPServer((BIND_HOST, port), make_handler(manager, port))
    if port == 0:  # 시험용: 실제로 잡힌 포트로 허용 목록을 다시 만든다
        server.RequestHandlerClass = make_handler(manager, server.server_address[1])
    server.daemon_threads = True
    return server


def main():
    raw = os.environ.get("VPN_GUI_PORT", str(DEFAULT_PORT))
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        print("VPN_GUI_PORT 는 1024~65535 의 숫자여야 합니다.", file=sys.stderr)
        return 2
    port = int(raw)
    try:
        server = make_server(port=port)
    except OSError as e:
        print(f"포트 {port} 를 열지 못했습니다: {e.strerror}", file=sys.stderr)
        return 1
    print(f"vpn-proxy 화면 → http://{BIND_HOST}:{port}  (끝내기: Ctrl+C)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
