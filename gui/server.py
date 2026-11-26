#!/usr/bin/env python3
"""vpn-proxy 웹 화면. Mac(호스트)에서 돌고, 이 저장소의 docker compose 를 대신 부른다.

  python3 gui/server.py            → http://127.0.0.1:8030
  VPN_GUI_PORT=8031 python3 gui/server.py

하는 일
  - compose 에 정의된 VPN(프로필) 목록, 상태, SOCKS5 포트, 최근 로그를 보여 준다.
  - 켜기 = docker compose up -d --no-build <서비스>,  끄기 = docker compose stop <서비스>.

지키는 것
  - 127.0.0.1 에만 연다(주소는 바꿀 수 없다. 포트만 바꾼다).
  - Host 머리말이 127.0.0.1/localhost 가 아니면 거절한다(다른 이름으로 이 주소를 가리키게 하는 공격 방지).
  - 상태를 바꾸는 요청은 POST 뿐이고, Origin 이 이 화면 자신일 때만 받는다. GET 은 아무것도 바꾸지 않는다.
  - 다룰 수 있는 대상은 compose 에 정의된 서비스 이름뿐이다. 요청 값은 그 목록과 비교만 한다.
  - ovpn 을 올리거나 고치는 기능은 없다. ovpn 검사(check-ovpn.sh)는 컨테이너가 시작할 때 그대로 돈다.
  - ovpn·auth.txt 의 내용은 읽지 않는다(.ovpn 파일이 몇 개인지만 센다). 로그는 가려서 보여 준다.
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
from urllib.parse import parse_qs, urlsplit

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
        rc, out, _ = self.docker.run(["compose", "config", "--format", "json"])
        if rc != 0:
            return None, "compose 설정을 읽지 못했습니다(docker compose config 실패). docker 가 설치돼 있는지, .env 값을 확인하세요."
        try:
            services = json.loads(out).get("services", {})
        except ValueError:
            return None, "compose 설정을 해석하지 못했습니다."
        result = []
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
                }
            )
        self._cache, self._cache_at = result, now
        return result, None

    def profile(self, profile_id):
        if not isinstance(profile_id, str) or not NAME_RE.match(profile_id):
            return None
        items, _ = self.profiles()
        for p in items or []:
            if p["id"] == profile_id:
                return p
        return None

    @staticmethod
    def _ovpn_count(folder):
        """폴더의 .ovpn 파일 수(내용은 읽지 않는다). 폴더가 없으면 None."""
        try:
            return sum(1 for f in os.scandir(folder) if f.is_file() and f.name.endswith(".ovpn"))
        except OSError:
            return None

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
        view = {
            "id": profile["id"],
            "name": profile["name"],
            "socks_port": profile["socks_port"],
            "socks_host": profile["socks_host"],
            "folder": self._folder_label(profile["folder"]),
            "ovpn_files": self._ovpn_count(profile["folder"]) if profile["folder"] else None,
            "state": OFF,
            "message": "",
            "since": "",
            "dns": [],
            "restarts": 0,
            "log": [],
        }
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
        return {
            "id": profile["id"],
            "name": profile["name"],
            "socks_port": profile["socks_port"],
            "socks_host": profile["socks_host"],
            "folder": self._folder_label(profile["folder"]),
            "ovpn_files": self._ovpn_count(profile["folder"]) if profile["folder"] else None,
            "state": UNKNOWN,
            "message": "Docker 가 꺼져 있어 상태를 알 수 없습니다.",
            "since": "",
            "dns": [],
            "restarts": 0,
            "log": [],
        }

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
                args = ["compose", "up", "-d", "--no-build", "--", profile["id"]]
            else:
                args = ["compose", "stop", "--", profile["id"]]
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
            if not m:
                return self._fail(404, "없는 주소입니다.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._fail(400, "잘못된 요청입니다.")
            if length < 0 or length > MAX_BODY:
                return self._fail(413, "요청이 너무 큽니다.")
            if length:
                self.rfile.read(length)  # 본문 값은 쓰지 않는다
            ok, message = manager.switch(m.group(1), m.group(2) == "on")
            self._send(200 if ok else 409, {"ok": ok, "message": message})

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
