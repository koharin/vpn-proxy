#!/usr/bin/env python3
"""웹 화면(gui/server.py) 시험. 진짜 docker 를 부르지 않는다 — 가짜 docker 명령을 만들어 쓴다.

  python3 tests/gui_test.py
"""
import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "gui"))
import server as gui  # noqa: E402

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "calls.raw"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
with open(os.path.join(d, "calls"), "a") as f:
    f.write(json.dumps([x for x in sys.argv[1:] if x != "--"]) + "\n")
state = json.load(open(os.path.join(d, "state.json")))
a = [x for x in sys.argv[1:] if x != "--"]
if state.get("daemon") is False and a[0] != "compose":
    sys.stderr.write("failed to connect to the docker API\n"); sys.exit(1)
if a[:2] == ["compose", "config"]:
    print(json.dumps(state["compose"])); sys.exit(0)
if a[0] == "version":
    print("27.0"); sys.exit(0)
if a[0] == "inspect":
    c = state["containers"].get(a[-1])
    if not c: sys.exit(1)
    print(json.dumps(c["State"]) + " " + str(c.get("restarts", 0))); sys.exit(0)
if a[0] == "exec":
    c = state["containers"].get(a[1])
    if not c or "status" not in c: sys.exit(1)
    sys.stdout.write(c["status"] if isinstance(c["status"], str) else json.dumps(c["status"])); sys.exit(0)
if a[0] == "logs":
    c = state["containers"].get(a[-1])
    if not c: sys.exit(1)
    sys.stdout.write(c.get("logs", "")); sys.exit(0)
if a[0] == "compose" and a[1] in ("up", "stop"):
    if state.get("daemon") is False:
        sys.stderr.write("Cannot connect to the Docker daemon\n"); sys.exit(1)
    if state.get("action_error"):
        sys.stderr.write(state["action_error"] + "\n"); sys.exit(1)
    sys.exit(0)
sys.exit(2)
'''


def compose(tmp):
    def svc(key, port):
        return {
            "container_name": "vpn-proxy-" + key[-1],
            "environment": {"VPN_NAME": key},
            "ports": [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 1080, "published": str(port), "protocol": "tcp"}],
            "volumes": [{"type": "bind", "source": str(Path(tmp) / "ovpn" / key), "target": "/vpn", "read_only": True}],
        }

    return {"services": {"vpn-a": svc("vpn-a", 1081), "vpn-b": svc("vpn-b", 1082), "vpn-c": svc("vpn-c", 1083), "bad name!": svc("vpn-x", 1)}}


class GuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vpn-proxy-gui-test.")
        fake = Path(self.tmp) / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        for n in ("vpn-a", "vpn-b"):
            (Path(self.tmp) / "ovpn" / n).mkdir(parents=True)
        (Path(self.tmp) / "ovpn" / "vpn-a" / "client.ovpn").write_text("client\n")
        (Path(self.tmp) / "ovpn" / "vpn-a" / "auth.txt").write_text("someone\nSECRET-IN-AUTH-FILE\n")
        self.state = {"daemon": True, "compose": compose(self.tmp), "containers": {}}
        self.save()
        os.environ["FAKE_DIR"] = self.tmp
        self.manager = gui.Manager(docker=gui.Docker(binary=str(fake), cwd=self.tmp), root=self.tmp)
        self.server = gui.make_server(self.manager, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self):
        (Path(self.tmp) / "state.json").write_text(json.dumps(self.state))
        if hasattr(self, "manager"):
            self.manager._cache = None
            self.manager.forget_overview()

    def calls(self):
        p = Path(self.tmp) / "calls"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def changing_calls(self):
        return [c for c in self.calls() if c[0] == "compose" and c[1] in ("up", "stop", "down", "rm", "restart", "kill")]

    def req(self, method, path, headers=None, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        return res, raw

    def post(self, path, origin="self", ctype="application/json", extra=None, host=None):
        h = {}
        if origin == "self":
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            h["Origin"] = origin
        if ctype:
            h["Content-Type"] = ctype
        h.update(extra or {})
        res, raw = self.req("POST", path, h, b"{}", host)
        return res.status, json.loads(raw)

    def overview(self):
        res, raw = self.req("GET", "/api/profiles")
        self.assertEqual(res.status, 200)
        return json.loads(raw)

    def by_id(self, data):
        return {p["id"]: p for p in data["profiles"]}

    # ── 바인드·머리말 ──────────────────────────────────────────────────────
    def test_binds_loopback_only(self):
        self.assertEqual(gui.BIND_HOST, "127.0.0.1")
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        # 이 기계의 다른 주소로는 붙지 않는다
        try:
            other = socket.gethostbyname(socket.gethostname())
        except OSError:
            other = "127.0.0.1"
        if not other.startswith("127."):
            s = socket.socket()
            s.settimeout(2)
            self.assertNotEqual(s.connect_ex((other, self.port)), 0)
            s.close()
        src = (REPO / "gui" / "server.py").read_text()
        self.assertNotIn("0.0.0.0", src)
        self.assertNotIn("VPN_GUI_HOST", src)

    def test_pages_and_security_headers(self):
        for path, word in (("/", b"vpn-proxy"), ("/app.js", b"textContent"), ("/style.css", b":root")):
            res, raw = self.req("GET", path)
            self.assertEqual(res.status, 200, path)
            self.assertIn(word, raw)
            self.assertIn("default-src 'none'", res.getheader("Content-Security-Policy"))
            self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
            self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
            self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        html = (REPO / "gui" / "static" / "index.html").read_text()
        self.assertNotIn("<script>", html)  # 인라인 스크립트 없음
        js = (REPO / "gui" / "static" / "app.js").read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, js)

    def test_rejects_foreign_host_header(self):
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1:1", "127.0.0.1.evil.example:%d" % self.port):
            res, _ = self.req("GET", "/api/profiles", host=host)
            self.assertEqual(res.status, 403, host)
            self.assertEqual(self.post("/api/profiles/vpn-a/on", host=host)[0], 403)
        res, _ = self.req("GET", "/api/profiles", host="localhost:%d" % self.port)
        self.assertEqual(res.status, 200)
        self.assertEqual(self.changing_calls(), [])

    # ── 상태를 바꾸는 요청 ─────────────────────────────────────────────────
    def test_post_needs_same_origin(self):
        bad = [None, "null", "http://evil.example", f"http://127.0.0.1:{self.port + 1}", f"https://127.0.0.1:{self.port}", f"http://127.0.0.1:{self.port}.evil.example", f"http://evil.example/http://127.0.0.1:{self.port}"]
        for origin in bad:
            status, body = self.post("/api/profiles/vpn-a/on", origin=origin)
            self.assertEqual(status, 403, origin)
            self.assertFalse(body["ok"])
        self.assertEqual(self.post("/api/profiles/vpn-a/on", extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.post("/api/profiles/vpn-a/on", ctype="text/plain")[0], 415)
        self.assertEqual(self.post("/api/profiles/vpn-a/on", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.changing_calls(), [])
        self.assertEqual(self.post("/api/profiles/vpn-a/on", origin=f"http://localhost:{self.port}", host=f"localhost:{self.port}")[0], 200)

    def test_get_never_changes_state(self):
        for path in ("/api/profiles/vpn-a/on", "/api/profiles/vpn-a/off", "/api/profiles", "/", "/api/profiles?on=vpn-a"):
            self.req("GET", path)
            self.req("HEAD", path)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            res, _ = self.req(method, "/api/profiles/vpn-a/on", {"Origin": f"http://127.0.0.1:{self.port}"})
            self.assertEqual(res.status, 405)
        self.assertEqual(self.changing_calls(), [])

    def test_on_off_only_for_compose_services(self):
        self.assertEqual(self.post("/api/profiles/vpn-a/on"), (200, {"ok": True, "message": "켰습니다. 연결 상태는 아래에 표시됩니다."}))
        self.assertEqual(self.post("/api/profiles/vpn-b/off")[0], 200)
        self.assertEqual(self.changing_calls(), [["compose", "up", "-d", "--no-build", "vpn-a"], ["compose", "stop", "vpn-b"]])
        before = len(self.changing_calls())
        for name in ("nope", "vpn-proxy-a", "--all", "-f", "..", "vpn-a;id", "vpn-a%20vpn-b", "%2e%2e", "VPN-A", "a" * 33):
            status, body = self.post(f"/api/profiles/{name}/on")
            self.assertIn(status, (404, 409), name)
            self.assertFalse(body["ok"])
        self.assertEqual(self.post("/api/profiles/vpn-a/on?x=1")[0], 400)
        self.assertEqual(self.post("/api/profiles/vpn-a/restart")[0], 404)
        self.assertEqual(self.post("/api/upload")[0], 404)  # 올리기 기능은 없다
        self.assertEqual(len(self.changing_calls()), before)

    def test_action_errors_are_plain_and_do_not_echo_docker_output(self):
        self.state["action_error"] = "Error: No such image: vpn-proxy:local /Users/someone/secret-path"
        self.save()
        status, body = self.post("/api/profiles/vpn-a/on")
        self.assertEqual(status, 409)
        self.assertIn("docker compose build", body["message"])
        self.assertNotIn("secret-path", json.dumps(body, ensure_ascii=False))
        self.state["action_error"] = "Bind for 127.0.0.1:1081 failed: port is already allocated"
        self.save()
        self.assertIn("포트", self.post("/api/profiles/vpn-a/on")[1]["message"])
        self.state["action_error"] = "something odd token=abc"
        self.save()
        self.assertNotIn("abc", self.post("/api/profiles/vpn-a/on")[1]["message"])

    # ── 목록·상태 ──────────────────────────────────────────────────────────
    def test_profile_list_ports_and_folders(self):
        data = self.overview()
        self.assertTrue(data["docker"])
        p = self.by_id(data)
        self.assertEqual(sorted(p), ["vpn-a", "vpn-b", "vpn-c"])  # 이름이 이상한 서비스는 빠진다
        self.assertEqual([p[k]["socks_port"] for k in sorted(p)], [1081, 1082, 1083])
        self.assertEqual(p["vpn-a"]["socks_host"], "127.0.0.1")
        self.assertEqual((p["vpn-a"]["ovpn_files"], p["vpn-b"]["ovpn_files"], p["vpn-c"]["ovpn_files"]), (1, 0, None))
        self.assertEqual(p["vpn-a"]["folder"], "ovpn/vpn-a")
        self.assertEqual({v["state"] for v in p.values()}, {"off"})
        # ovpn·auth.txt 내용과 절대 경로는 응답에 없다
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("SECRET-IN-AUTH-FILE", text)
        self.assertNotIn(self.tmp, text)

    def test_states(self):
        run = {"Running": True, "Restarting": False}
        st = lambda s, m: {"name": "x", "state": s, "since": "2026-11-26T01:00:00Z", "gen": 1, "dns": ["10.0.0.2", "<b>x</b>"], "socks_port": 1080, "message": m}
        self.state["containers"] = {
            "vpn-proxy-a": {"State": run, "status": st("up", "터널 연결됨"), "logs": "[vpn-a] SOCKS5 시작: 컨테이너 포트 1080 (터널 연결됨)\n"},
            "vpn-proxy-b": {"State": run, "restarts": 3, "status": st("connecting", "VPN 서버에 접속 중"), "logs": "[vpn-b] 오류: 60초 안에 VPN 이 연결되지 않았습니다.\n[vpn-b] OpenVPN 시작: c.ovpn\n"},
            "vpn-proxy-c": {"State": {"Running": False, "Restarting": True}, "restarts": 5, "logs": "[vpn-c] 오류: ovpn 파일이 없습니다. 호스트의 ovpn/<이름>/ 폴더에 .ovpn 파일 1개를 넣으세요.\n"},
        }
        self.save()
        p = self.by_id(self.overview())
        self.assertEqual((p["vpn-a"]["state"], p["vpn-a"]["message"], p["vpn-a"]["dns"]), ("up", "터널 연결됨", ["10.0.0.2"]))
        self.assertEqual(p["vpn-a"]["since"], "2026-11-26T01:00:00Z")
        self.assertEqual(p["vpn-b"]["state"], "connecting")
        self.assertIn("직전 시도 실패: 60초 안에", p["vpn-b"]["message"])
        self.assertEqual(p["vpn-c"]["state"], "failed")
        self.assertIn("ovpn 파일이 없습니다", p["vpn-c"]["message"])
        # 실패 대기 중(컨테이너는 살아 있고 state=stopped)
        self.state["containers"]["vpn-proxy-a"]["status"] = st("stopped", "이 ovpn 으로는 시작하지 않습니다.")
        # 멈춘 컨테이너 = 꺼짐, status.json 이 깨져 있으면 연결 중으로 본다
        self.state["containers"]["vpn-proxy-b"] = {"State": {"Running": False, "Restarting": False}, "logs": ""}
        self.state["containers"]["vpn-proxy-c"] = {"State": run, "status": "not json", "logs": ""}
        self.save()
        p = self.by_id(self.overview())
        self.assertEqual(p["vpn-a"]["state"], "failed")
        self.assertIn("이 ovpn 으로는 시작하지 않습니다", p["vpn-a"]["message"])
        self.assertEqual(p["vpn-b"]["state"], "off")
        self.assertEqual(p["vpn-c"]["state"], "connecting")
        # 상태 파일이 아직 없는데(설정 확인에서 멈춤) 로그 마지막 줄이 오류면 실패와 사유를 보여 준다
        self.state["containers"]["vpn-proxy-c"] = {"State": run, "logs": "[vpn-c] 오류: ovpn 파일이 없습니다. 폴더에 .ovpn 파일 1개를 넣으세요.\n"}
        self.save()
        c = self.by_id(self.overview())["vpn-c"]
        self.assertEqual(c["state"], "failed")
        self.assertIn("ovpn 파일이 없습니다", c["message"])
        self.assertIn("자동으로 다시 시도", c["message"])
        # 오류 뒤에 새 줄이 있으면(다시 시도 중) 연결 중이다
        self.state["containers"]["vpn-proxy-c"]["logs"] += "[vpn-c] 방화벽 적용: 터널과 VPN 서버 1곳만 허용\n"
        self.save()
        self.assertEqual(self.by_id(self.overview())["vpn-c"]["state"], "connecting")

    def test_docker_down_is_reported_not_crashed(self):
        self.state["daemon"] = False
        self.save()
        data = self.overview()
        self.assertFalse(data["docker"])
        self.assertIn("Docker 가 꺼져 있습니다", data["error"])
        self.assertEqual({p["state"] for p in data["profiles"]}, {"unknown"})
        self.assertEqual(len(data["profiles"]), 3)
        status, body = self.post("/api/profiles/vpn-a/on")
        self.assertEqual((status, body["message"]), (409, "Docker 가 꺼져 있습니다."))

    # ── 로그 가리기 ────────────────────────────────────────────────────────
    def test_logs_are_redacted_and_bounded(self):
        noisy = "\n".join(
            [
                "line %d" % i for i in range(100)
            ]
            + [
                "2026-11-26 01:00:00 password: hunter2-PLAIN",
                "2026-11-26 01:00:01 auth-token SESS_abcdef",
                "-----BEGIN PRIVATE KEY-----",
                "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj",
                "-----END PRIVATE KEY-----",
                "\x1b[31mred\x1b[0m and \x07bell",
                "x" * 1000 + " END",
                "2026-11-26 01:00:02 VERIFY OK: CN=<img src=x onerror=alert(1)>",
            ]
        )
        self.state["containers"] = {"vpn-proxy-a": {"State": {"Running": True, "Restarting": False}, "status": {"state": "up", "message": "secret=TOPSECRET ok"}, "logs": noisy}}
        self.save()
        a = self.by_id(self.overview())["vpn-a"]
        text = "\n".join(a["log"])
        self.assertLessEqual(len(a["log"]), gui.LOG_LINES)
        for leaked in ("hunter2-PLAIN", "SESS_abcdef", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj", "BEGIN PRIVATE KEY", "\x1b", "\x07"):
            self.assertNotIn(leaked, text)
        self.assertIn("red and bell", text)
        self.assertTrue(all(len(l) <= gui.LOG_LINE_MAX + 1 for l in a["log"]))
        self.assertIn("<img src=x onerror=alert(1)>", text)  # 글자 그대로 준다(화면은 textContent 로만 넣는다)
        self.assertNotIn("TOPSECRET", a["message"])

    # ── 보안 검토 W1~W4 ────────────────────────────────────────────────────
    def test_w1_cross_site_get_is_refused_and_overview_is_cached(self):
        self.state["containers"] = {"vpn-proxy-a": {"State": {"Running": True, "Restarting": False}, "status": {"state": "up"}, "logs": ""}}
        self.save()
        for site in ("cross-site", "same-site", "weird"):
            res, _ = self.req("GET", "/api/profiles", {"Sec-Fetch-Site": site, "Origin": "http://evil.example"})
            self.assertEqual(res.status, 403, site)
            self.assertEqual(self.req("GET", "/api/health", {"Sec-Fetch-Site": site})[0].status, 403)
        self.assertEqual(self.calls(), [])  # 거절된 요청은 docker 를 부르지 않는다
        for site in ("same-origin", "none"):
            self.assertEqual(self.req("GET", "/api/profiles", {"Sec-Fetch-Site": site})[0].status, 200)
        self.assertEqual(self.req("GET", "/", {"Sec-Fetch-Site": "cross-site"})[0].status, 200)  # 페이지 자체는 열린다(주소창·링크)
        # 캐시: 짧은 시간 안의 반복·동시 요청은 docker 를 다시 부르지 않는다
        once = len(self.calls())
        self.assertGreater(once, 0)
        threads = [threading.Thread(target=self.overview) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(self.calls()), once)
        self.assertLessEqual(gui.OVERVIEW_TTL, 2)
        # 시간이 지나면 다시 계산하고, 켜기·끄기 뒤에는 바로 새로 읽는다
        self.manager._view_at -= gui.OVERVIEW_TTL + 0.1
        self.overview()
        self.assertEqual(len(self.calls()), once * 2 - 1)  # compose 설정은 따로 5초 캐시라 한 번 덜 부른다
        self.post("/api/profiles/vpn-a/off")
        before = len(self.calls())
        self.overview()
        self.assertGreater(len(self.calls()), before)

    def test_w2_log_redaction_shapes(self):
        S = "S3cr3tValue"
        shapes = [
            f"Authorization: Bearer {S}",
            f"Authorization: Basic {S}",
            f"proxy-authorization: Digest username=x, response={S}",
            f"the password is {S}",
            f'password="first {S}"',
            f"connecting to socks5://user:{S}@10.0.0.1:1080",
            f"pwd={S}",
            f"pw: {S}",
            f"credentials: {S}",
            f"private_key={S}",
            f"Set-Cookie: session={S}; Path=/",
            f"MY_PASSWORD={S}",
            f"DB_PASSWORD_FILE: {S}",
            f"api-key {S}",
            f"Private Key: {S}",
        ]
        for line in shapes:
            self.assertNotIn(S, gui.redact_line(line), line)
        self.assertEqual(gui.redact_line(f"x://user:{S}@h/p"), "x://user:[가림]@h/p")
        # 본문 마지막의 짧은 줄까지 통째로 빠진다
        pem = "before\n-----BEGIN CERTIFICATE-----\nMIIBshortline==\nQUJDRA==\n-----END CERTIFICATE-----\nafter"
        self.assertEqual(gui.redact_log(pem), ["before", "[가림: 인증서·키 본문]", "after"])
        # 시작 줄이 잘려 나간 경우와 끝 줄이 아직 없는 경우
        self.assertEqual(gui.redact_log("keep this line\nQUJDRUZHSElK\nQUJDRA==\n-----END PRIVATE KEY-----\nafter"), ["keep this line", "[가림: 인증서·키 본문]", "after"])
        self.assertEqual(gui.redact_log("a\n-----BEGIN PRIVATE KEY-----\nQUJDRA==\nQUJD"), ["a", "[가림: 인증서·키 본문]"])
        # 평범한 줄은 그대로 둔다
        for plain in (
            "[vpn-a] SOCKS5 시작: 컨테이너 포트 1080 (터널 연결됨)",
            "2026-11-26 01:00:00 Initialization Sequence Completed",
            "[vpn-a] 오류: 이 ovpn 은 아이디·비밀번호가 필요합니다(auth-user-pass). ovpn 과 같은 폴더에 auth.txt 를 넣으세요.",
            "2026-11-26 01:00:00 TCP/UDP: Preserving recently used remote address: [AF_INET]10.0.0.1:1194",
            "bypass compass passenger tokenizer",
        ):
            self.assertEqual(gui.redact_line(plain), plain)
        readme = (REPO / "README.md").read_text()
        self.assertIn("알려진 모양", readme)
        self.assertIn("모든 비밀을 찾아내지는 못하므로", readme)

    def test_w3_names_starting_with_dash_are_not_used(self):
        services = self.state["compose"]["services"]
        services["-x"] = dict(services["vpn-a"], container_name="vpn-proxy-x")
        services["--help"] = dict(services["vpn-a"], container_name="vpn-proxy-h")
        services["vpn-d"] = dict(services["vpn-a"], container_name="-rf")
        services[".hidden"] = dict(services["vpn-a"], container_name="vpn-proxy-hid")
        self.save()
        self.assertEqual(sorted(self.by_id(self.overview())), ["vpn-a", "vpn-b", "vpn-c"])
        for name in ("-x", "--help", "vpn-d", ".hidden", "-", "_a"):
            status, body = self.post(f"/api/profiles/{name}/on")
            self.assertIn(status, (404, 409), name)
        self.assertEqual(self.changing_calls(), [])
        # 이름 앞에는 항상 '--' 를 둔다(이름이 옵션으로 읽히지 않게)
        self.state["containers"] = {"vpn-proxy-a": {"State": {"Running": True, "Restarting": False}, "status": {"state": "up"}, "logs": ""}}
        self.save()
        self.overview()
        self.post("/api/profiles/vpn-a/on")
        self.post("/api/profiles/vpn-a/off")
        raw = [json.loads(l) for l in (Path(self.tmp) / "calls.raw").read_text().splitlines()]
        named = [c for c in raw if c[0] in ("inspect", "logs", "exec") or c[:2] in (["compose", "up"], ["compose", "stop"])]
        self.assertEqual({c[0] if c[0] != "compose" else c[1] for c in named}, {"inspect", "logs", "exec", "up", "stop"})
        for c in named:
            i = c.index("--")
            self.assertIn(c[i + 1], ("vpn-a", "vpn-proxy-a", "vpn-proxy-b", "vpn-proxy-c"), c)
            self.assertFalse(any(x in ("vpn-a", "vpn-proxy-a") for x in c[:i]), c)

    def test_w4_half_sent_request_is_dropped(self):
        self.assertEqual(self.server.RequestHandlerClass.timeout, gui.REQUEST_TIMEOUT)
        self.assertLessEqual(gui.REQUEST_TIMEOUT, 10)
        self.server.RequestHandlerClass.timeout = 1  # 시험에서는 짧게
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"GET /api/profiles HTTP/1.1\r\nHost: 127.0.0.1")  # 머리말을 끝내지 않는다
            s.settimeout(5)
            self.assertEqual(s.recv(1024), b"")  # 서버가 연결을 끊는다
            s.close()
        finally:
            self.server.RequestHandlerClass.timeout = gui.REQUEST_TIMEOUT
        self.assertEqual(self.overview()["ok"], True)  # 그 뒤에도 정상 응답

    def test_long_lines_are_cut_before_redaction(self):
        import time

        self.assertLessEqual(gui.LINE_SCAN_MAX, 2000)
        n = 300_000
        hostile = [
            "a_" * n,  # 밑줄로 이어진 이름 조각
            "a-" * n + "password",
            "x://" + "a" * n,
            "A" * n + " ",
            "pass " * n,
            "-----BEGIN " + "A " * n,
            "\x1b[" + "1;" * n,
            ("password=S3cr3tValue " + "b_" * n),
        ]
        start = time.monotonic()
        for line in hostile:
            out = gui.redact_line(line)
            self.assertLessEqual(len(out), gui.LOG_LINE_MAX + 1)
            self.assertNotIn("S3cr3tValue", out)
        lines = gui.redact_log("\n".join(hostile * 5))
        self.assertLess(time.monotonic() - start, 2.0)  # 자르지 않으면 수십 초가 걸린다
        self.assertTrue(all(len(l) <= gui.LOG_LINE_MAX + 1 for l in lines))
        # 잘린 뒤쪽에 있던 값은 화면에 나가지 않는다
        self.assertNotIn("TAILSECRET", gui.redact_line("x" * 5000 + " password=TAILSECRET"))

    def test_repo_files(self):
        self.assertIn("!rootfs/", (REPO / ".dockerignore").read_text())  # 화면 코드는 이미지에 들어가지 않는다
        src = (REPO / "gui" / "server.py").read_text()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        # ovpn·auth 파일을 여는 코드가 없다(파일 수만 센다)
        self.assertNotIn("open(", src)
        self.assertEqual(src.count("read_bytes"), 1)  # 정적 파일 한 곳뿐


if __name__ == "__main__":
    unittest.main(verbosity=2)
