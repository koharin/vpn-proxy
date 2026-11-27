#!/usr/bin/env python3
"""웹 화면(gui/server.py) 시험. 진짜 docker 를 부르지 않는다 — 가짜 docker 명령을 만들어 쓴다.

  python3 tests/gui_test.py
"""
import http.client
import json
import os
import socket
import re
import shutil
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
files = []
while len(a) > 2 and a[1] == "-f":   # compose -f 파일 … 은 떼어 두고 본다
    files.append(a[2]); del a[1:3]
if state.get("daemon") is False and a[0] != "compose":
    sys.stderr.write("failed to connect to the docker API\n"); sys.exit(1)
if a[:2] == ["compose", "config"]:
    if state.get("config_error") and files: sys.exit(1)
    cfg = state["compose"]
    for name in files[1:]:   # 생성 파일(JSON)의 서비스를 docker compose config 가 내는 모양으로 옮겨 합친다
        for key, svc in json.load(open(name))["services"].items():
            ip, pub, tgt = svc["ports"][0].split(":")
            src, dst, _ = svc["volumes"][0].split(":")
            cfg["services"][key] = {"container_name": svc["container_name"], "environment": svc["environment"],
                "ports": [{"host_ip": ip, "published": pub, "target": int(tgt)}],
                "volumes": [{"type": "bind", "source": os.path.abspath(src), "target": dst, "read_only": True}]}
    if "--quiet" not in a: print(json.dumps(cfg))
    sys.exit(0)
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
if a[0] == "compose" and a[1] in ("up", "stop", "rm"):
    if state.get("daemon") is False:
        sys.stderr.write("Cannot connect to the Docker daemon\n"); sys.exit(1)
    if state.get("action_error"):
        sys.stderr.write(state["action_error"] + "\n"); sys.exit(1)
    sys.exit(0)
sys.exit(2)
'''


KEY_MARK = "FAKEKEYBODYDONOTLEAK"
PW_MARK = "Fake-Pw-DoNotLeak-9"
# 가짜 ovpn: 인증서·키가 블록으로 들어 있다(내용은 가짜다).
FAKE_OVPN = """client
dev tun
proto udp
remote vpn.example.invalid 1194
nobind
persist-key
remote-cert-tls server
cipher AES-256-GCM
<ca>
-----BEGIN CERTIFICATE-----
FAKECABODY
-----END CERTIFICATE-----
</ca>
<cert>
-----BEGIN CERTIFICATE-----
FAKECERTBODY
-----END CERTIFICATE-----
</cert>
<key>
-----BEGIN PRIVATE KEY-----
%s
-----END PRIVATE KEY-----
</key>
<tls-crypt>
-----BEGIN OpenVPN Static key V1-----
0123456789abcdef
-----END OpenVPN Static key V1-----
</tls-crypt>
""" % KEY_MARK


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

    def test_title_is_ovpn_name_and_subtitle_is_server(self):
        d = Path(self.tmp) / "ovpn"
        (d / "vpn-c").mkdir()
        (d / "vpn-a" / "client.ovpn").rename(d / "vpn-a" / "ENKI 1.ovpn")  # 공백이 든 이름
        (d / "vpn-a" / "ENKI 1.ovpn").write_text(
            "client\r\nproto udp4\r\n<ca>\nremote in-ca.example 1 udp\nCA-BODY-SECRET\n</ca>\n"
            "remote 192.0.2.10 1194\r\n<tls-crypt>\nTLS-CRYPT-SECRET\n</tls-crypt>\n<key>\nKEY-BODY-SECRET\n</key>\n"
            "auth-user-pass\nverify-x509-name SERVER-CN-NOT-SHOWN name\n"
        )
        (d / "vpn-b" / "apex2026_gt_someone.name.ovpn").write_text(
            "remote vpn.example.net 443 tcp\nremote second.example.net\n<connection>\nremote third.example.net 80\n</connection>\nport 5000\n"
        )
        (d / "vpn-c" / "one.ovpn").write_text("remote 192.0.2.1\n")
        (d / "vpn-c" / "two.ovpn").write_text("remote 192.0.2.2\n")
        self.save()
        data = self.overview()
        p = self.by_id(data)
        self.assertEqual((p["vpn-a"]["title"], p["vpn-a"]["server"], p["vpn-a"]["server_more"]), ("ENKI 1", "192.0.2.10:1194/udp4", 0))
        self.assertEqual((p["vpn-b"]["title"], p["vpn-b"]["server"], p["vpn-b"]["server_more"]), ("apex2026_gt_someone.name", "vpn.example.net:443/tcp", 2))
        # .ovpn 이 2개면 어느 것인지 알 수 없으니 폴더 이름을 제목으로 두고 서버는 비운다
        self.assertEqual((p["vpn-c"]["title"], p["vpn-c"]["server"], p["vpn-c"]["ovpn_files"]), ("vpn-c", "", 2))
        # 폴더 이름과 로컬 포트는 그대로 따로 온다(화면에서 작게 보인다)
        self.assertEqual((p["vpn-a"]["name"], p["vpn-a"]["socks_port"], p["vpn-a"]["folder"]), ("vpn-a", 1081, "ovpn/vpn-a"))
        # 인증서·키 블록 안쪽과 remote·port·proto 가 아닌 줄은 응답에 없다
        text = json.dumps(data, ensure_ascii=False)
        for secret in ("CA-BODY-SECRET", "TLS-CRYPT-SECRET", "KEY-BODY-SECRET", "in-ca.example", "SERVER-CN-NOT-SHOWN", "SECRET-IN-AUTH-FILE"):
            self.assertNotIn(secret, text)
        # Docker 가 꺼져 있어도 제목·서버는 보인다
        self.state["daemon"] = False
        self.save()
        p = self.by_id(self.overview())
        self.assertEqual((p["vpn-a"]["title"], p["vpn-a"]["server"], p["vpn-a"]["state"]), ("ENKI 1", "192.0.2.10:1194/udp4", "unknown"))

    def test_ovpn_target_takes_only_well_formed_values(self):
        f = Path(self.tmp) / "t.ovpn"
        cases = {
            "remote host.example\n": ("host.example:1194/udp", 0),
            "port 443\nproto tcp-client\nremote host.example\n": ("host.example:443/tcp-client", 0),
            "remote 2001:db8::1 1194 udp6\n": ("[2001:db8::1]:1194/udp6", 0),
            "  remote\thost.example  99999 weird\n": ("host.example:1194/udp", 0),  # 이상한 포트·프로토콜은 버린다
            "remote <script>alert(1)</script> 1194\n# remote c.example\n;remote d.example\n": ("", 0),
            "remote-cert-tls server\ndev tun\n": ("", 0),
            "<key>\nremote hidden.example 1\n": ("", 0),  # 닫히지 않은 블록
            "": ("", 0),
        }
        for body, want in cases.items():
            f.write_text(body)
            self.assertEqual(gui.ovpn_target(str(f)), want, body)
        self.assertEqual(gui.ovpn_target(str(Path(self.tmp) / "none.ovpn")), ("", 0))
        link = Path(self.tmp) / "link.ovpn"
        f.write_text("remote host.example\n")
        link.symlink_to(f)
        self.assertEqual(gui.ovpn_target(str(link)), ("", 0))  # 링크는 따라가지 않는다
        # 화면은 제목·서버를 글자로만 넣는다
        js = (REPO / "gui" / "static" / "app.js").read_text()
        self.assertIn("p.title || p.name", js)
        self.assertIn("p.server", js)
        self.assertNotIn("innerHTML", js)

    def test_v1_odd_port_or_unreadable_ovpn_blanks_only_that_profile(self):
        f = Path(self.tmp) / "t.ovpn"
        # isdigit() 은 참이지만 [0-9] 가 아닌 글자, 범위 밖 숫자는 포트로 쓰지 않는다
        cases = {
            "port \u00b2\nremote h.example.com\n": "h.example.com:1194/udp",
            "remote h.example.com \u00b2 udp\n": "h.example.com:1194/udp",
            "remote h.example.com \u0661\u0662\u0663\n": "h.example.com:1194/udp",
            "remote h.example.com 0\n": "h.example.com:1194/udp",
            "remote h.example.com 65536\n": "h.example.com:1194/udp",
            "remote h.example.com 123456\n": "h.example.com:1194/udp",
            "remote h.example.com 65535\n": "h.example.com:65535/udp",
        }
        for body, want in cases.items():
            f.write_text(body, encoding="utf-8")
            self.assertEqual(gui.ovpn_target(str(f)), (want, 0), body)
        d = Path(self.tmp) / "ovpn"
        (d / "vpn-a" / "client.ovpn").write_text("remote a.example.com \u00b2 udp\nport \u00b2\n", encoding="utf-8")
        (d / "vpn-b" / "b.ovpn").write_text("remote b.example.com 443 tcp\n")
        self.save()
        p = self.by_id(self.overview())
        self.assertEqual((p["vpn-a"]["server"], p["vpn-b"]["server"]), ("a.example.com:1194/udp", "b.example.com:443/tcp"))
        # ovpn 읽기가 어떤 이유로든 실패해도 그 프로필의 서버만 비고 나머지는 그대로 보인다
        real = gui.ovpn_target

        def broken(path):
            if os.path.basename(path) == "client.ovpn":
                raise ValueError("broken ovpn")
            return real(path)

        gui.ovpn_target = broken
        try:
            self.save()
            p = self.by_id(self.overview())
        finally:
            gui.ovpn_target = real
        self.assertEqual((p["vpn-a"]["title"], p["vpn-a"]["server"], p["vpn-a"]["server_more"]), ("client", "", 0))
        self.assertEqual((p["vpn-b"]["title"], p["vpn-b"]["server"]), ("b", "b.example.com:443/tcp"))

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

    # ── ovpn 올리기·삭제 ───────────────────────────────────────────────────
    def send(self, path, body, extra=None):
        h = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        h.update(extra or {})
        res, raw = self.req("POST", path, h, body if isinstance(body, bytes) else json.dumps(body).encode())
        return res.status, json.loads(raw)

    def upload(self, name="office", ovpn=None, **kw):
        return self.send("/api/vpns", {"name": name, "ovpn": FAKE_OVPN if ovpn is None else ovpn, **kw})

    def use_checker(self):
        """진짜 검사기(rootfs/check-ovpn.sh)를 시험 폴더에 둔다. 포트는 모두 비어 있다고 본다."""
        (Path(self.tmp) / "rootfs").mkdir(exist_ok=True)
        shutil.copy(REPO / "rootfs" / "check-ovpn.sh", Path(self.tmp) / "rootfs" / "check-ovpn.sh")
        (Path(self.tmp) / "docker-compose.yml").write_text("services: {}\n")
        self.manager.store.port_free = lambda port: True

    def mode(self, *parts):
        return stat.S_IMODE(os.lstat(Path(self.tmp).joinpath(*parts)).st_mode)

    def test_upload_registers_a_new_vpn(self):
        self.use_checker()
        status, data = self.upload()
        self.assertEqual((status, data["ok"], data["reasons"]), (200, True, []))
        self.assertIn("127.0.0.1:1084", data["message"])  # 1081~1083 은 기존 VPN 이 쓴다
        self.assertNotIn(KEY_MARK, json.dumps(data))
        root = Path(self.tmp)
        self.assertEqual((root / "ovpn" / "office" / "office.ovpn").read_text(), FAKE_OVPN)
        self.assertEqual((self.mode("ovpn", "office"), self.mode("ovpn", "office", "office.ovpn")), (0o700, 0o600))
        self.assertEqual((self.mode("vpns.json"), self.mode("compose.generated.yml")), (0o600, 0o600))
        self.assertFalse((root / "ovpn" / "office" / "auth.txt").exists())
        self.assertEqual(json.loads((root / "vpns.json").read_text()), {"vpns": [{"name": "office", "port": 1084, "folder": "ovpn/office"}]})
        svc = json.loads((root / "compose.generated.yml").read_text())["services"]["office"]
        self.assertEqual((svc["ports"], svc["volumes"], svc["container_name"]), (["127.0.0.1:1084:1080"], ["./ovpn/office:/vpn:ro"], "vpn-proxy-office"))
        self.assertEqual((svc["cap_drop"], svc["security_opt"]), (["ALL"], ["no-new-privileges:true"]))
        p = self.by_id(self.overview())["office"]
        self.assertEqual((p["managed"], p["socks_port"], p["socks_host"], p["title"], p["server"]), (True, 1084, "127.0.0.1", "office", "vpn.example.invalid:1194/udp"))
        self.assertFalse(self.by_id(self.overview())["vpn-a"]["managed"])
        self.assertNotIn(KEY_MARK, json.dumps(self.overview()))
        # 켜기·끄기는 생성 파일을 -f 로 함께 쓴다
        self.assertEqual(self.post("/api/profiles/office/on")[0], 200)
        self.assertEqual(self.calls()[-1], ["compose", "-f", "docker-compose.yml", "-f", "compose.generated.yml", "up", "-d", "--no-build", "office"])
        # 다음 것은 다음 빈 포트
        self.assertIn("127.0.0.1:1085", self.upload("lab-2")[1]["message"])

    def test_upload_with_username_and_password(self):
        self.use_checker()
        ovpn = FAKE_OVPN + "auth-user-pass\n"
        status, data = self.upload(ovpn=ovpn)
        self.assertEqual((status, data["ok"]), (409, False))
        self.assertIn("아이디·비밀번호가 필요", data["message"])
        self.assertFalse((Path(self.tmp) / "ovpn" / "office").exists())
        for user, pw in (("someone", "line1\nline2"), ("a\rb", "pw"), ("someone", "비밀"), ("x" * 257, "pw"), ("someone", "pw\n"), ("someone\n", "pw")):
            self.assertEqual(self.upload(ovpn=ovpn, username=user, password=pw)[0], 409, (user, pw))
        status, data = self.upload(ovpn=ovpn, username="someone", password=PW_MARK)
        self.assertEqual((status, data["ok"]), (200, True))
        auth = Path(self.tmp) / "ovpn" / "office" / "auth.txt"
        self.assertEqual((auth.read_text(), self.mode("ovpn", "office", "auth.txt")), ("someone\n" + PW_MARK + "\n", 0o600))
        everything = json.dumps(data) + json.dumps(self.overview()) + (Path(self.tmp) / "vpns.json").read_text() \
            + (Path(self.tmp) / "compose.generated.yml").read_text() + (Path(self.tmp) / "calls.raw").read_text()
        self.assertNotIn(PW_MARK, everything)
        self.assertNotIn("someone", everything)
        # 블록으로 든 아이디·비밀번호면 칸을 채우지 않아도 된다. 필요 없는 비밀번호는 저장하지 않는다
        inline = FAKE_OVPN + "auth-user-pass\n<auth-user-pass>\nu\np\n</auth-user-pass>\n"
        self.assertEqual(self.upload("inline", ovpn=inline)[0], 200)
        self.assertEqual(self.upload("plain", password=PW_MARK, username="someone")[0], 200)
        for name in ("inline", "plain"):
            self.assertFalse((Path(self.tmp) / "ovpn" / name / "auth.txt").exists())

    def test_upload_is_checked_like_the_container_does(self):
        self.use_checker()
        cases = {
            "명령 실행": (FAKE_OVPN + "up /bin/sh\n", "받지 않는 지시 \"up\""),
            "스크립트": (FAKE_OVPN + "script-security 2\n", "받지 않는 지시"),
            "setenv opt": (FAKE_OVPN + "setenv opt plugin x.so\n", "받지 않는 지시 \"plugin\""),
            "받지 않는 블록": (FAKE_OVPN + "<up>\nx\n</up>\n", "받지 않는 블록"),
            "닫히지 않은 블록": (FAKE_OVPN + "<key>\nabc\n", "닫히지 않았습니다"),
            "긴 줄": (FAKE_OVPN + "remote " + "a" * 300 + "\n", "255자"),
        }
        for label, (ovpn, why) in cases.items():
            status, data = self.upload(ovpn=ovpn)
            self.assertEqual((status, data["ok"]), (409, False), label)
            self.assertIn("검사를 통과하지 못했습니다", data["message"], label)
            self.assertTrue(any(why in r for r in data["reasons"]), (label, data["reasons"]))
            self.assertTrue(any("번째 줄" in r for r in data["reasons"]), label)   # 줄 번호
            self.assertFalse((Path(self.tmp) / "ovpn" / "office").exists(), label)   # 거절하면 남기지 않는다
        self.assertFalse((Path(self.tmp) / "vpns.json").exists())
        self.assertFalse((Path(self.tmp) / "compose.generated.yml").exists())
        # 바깥 파일을 가리키는 줄은 올릴 수 없다(올리는 것은 파일 하나뿐). 줄 번호를 알려 준다
        n = FAKE_OVPN.count("\n")
        for extra in ("ca ca.crt\n", "auth-user-pass creds.txt\n", "setenv opt tls-crypt tc.key\n", "<connection>\nremote h 1\nkey /etc/passwd\n</connection>\n"):
            status, data = self.upload(ovpn=FAKE_OVPN + extra)
            self.assertEqual(status, 409, extra)
            self.assertIn("다른 파일을 가리키는 줄", data["message"])
            self.assertRegex(data["reasons"][0], r"^%d번째 줄: " % (n + (3 if extra.startswith("<") else 1)))
        self.assertEqual(self.upload(ovpn=FAKE_OVPN + "dh none\n")[0], 200)   # dh none 은 파일이 아니다
        # 한글·NUL 등 ASCII 밖 글자, 빈 파일, 큰 파일
        for ovpn, why in ((FAKE_OVPN + "# 한글\n", "받지 않는 글자"), ("", "비어"), (" \n", "비어"), (FAKE_OVPN + "#" * gui.vpnstore.OVPN_MAX, "너무 큽니다")):
            status, data = self.upload("other", ovpn=ovpn)
            self.assertEqual(status, 409, why)
            self.assertIn(why, data["message"])
        status, data = self.upload("nul", ovpn=FAKE_OVPN + "\x00\n")
        self.assertEqual(status, 409)
        self.assertTrue(any("NUL" in r or "받지 않는 글자" in r for r in data["reasons"]), data)
        # 거절 사유에 올린 값이 길게 실려 나가지 않는다(가리고 자른다)
        status, data = self.upload("echo", ovpn=FAKE_OVPN + "setenv PASSWORD=" + PW_MARK + " x\n" + "setenv " + "A" * 200 + "\n")
        self.assertEqual(status, 409)
        self.assertNotIn(PW_MARK, json.dumps(data, ensure_ascii=False))
        self.assertNotIn("A" * 48, json.dumps(data, ensure_ascii=False))

    def test_upload_names_and_paths(self):
        self.use_checker()
        for bad in ("", "-a", "a-", "A", "a_b", "a.b", "../x", "a/b", "..", "a b", "x" * 25, "vpn\u00e9", "a\n", "a\x00"):
            status, data = self.upload(bad)
            self.assertEqual((status, data["ok"]), (409, False), repr(bad))
        self.assertEqual(sorted(p.name for p in (Path(self.tmp) / "ovpn").iterdir()), ["vpn-a", "vpn-b"])
        self.assertEqual(sorted(p.name for p in Path(self.tmp).parent.glob("x")), [])
        # 이미 있는 서비스·폴더·컨테이너 이름
        for taken, why in (("vpn-a", "이미 있는 이름"), ("a", "이미 있는 이름"), ("vpn-b", "이미 있는 이름")):
            self.assertIn(why, self.upload(taken)[1]["message"])
        (Path(self.tmp) / "ovpn" / "left").mkdir()
        self.assertIn("폴더가 이미 있습니다", self.upload("left")[1]["message"])
        os.symlink(self.tmp, Path(self.tmp) / "ovpn" / "link")
        self.assertIn("폴더가 이미 있습니다", self.upload("link")[1]["message"])
        self.assertEqual(self.upload()[0], 200)
        self.assertIn("이미 있는 이름", self.upload()[1]["message"])
        self.assertEqual((Path(self.tmp) / "ovpn" / "office" / "office.ovpn").read_text(), FAKE_OVPN)   # 덮어쓰지 않는다
        # 요청 모양: 글자열이 아닌 값, 목록, 깨진 JSON
        for body in ({"name": "x", "ovpn": ["client"]}, {"name": 1, "ovpn": "client\n"}, {"name": "x", "ovpn": FAKE_OVPN, "password": {"a": 1}}, ["x"], b"{", b"\xff"):
            self.assertIn(self.send("/api/vpns", body)[0], (400, 409), body)
        self.assertFalse((Path(self.tmp) / "ovpn" / "x").exists())

    def test_upload_needs_same_origin_and_fits_the_size_limit(self):
        self.use_checker()
        body = json.dumps({"name": "office", "ovpn": FAKE_OVPN}).encode()
        for headers in ({"Content-Type": "application/json"}, {"Origin": "http://evil.example", "Content-Type": "application/json"},
                        {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "text/plain"},
                        {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}):
            res, _ = self.req("POST", "/api/vpns", headers, body)
            self.assertIn(res.status, (403, 415), headers)
        res, _ = self.req("POST", "/api/vpns", {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}, body, host="evil.example")
        self.assertEqual(res.status, 403)
        self.assertEqual(self.req("GET", "/api/vpns")[0].status, 404)
        self.assertFalse((Path(self.tmp) / "ovpn" / "office").exists())
        big = json.dumps({"name": "office", "ovpn": "#" * (gui.UPLOAD_MAX_BODY + 10)}).encode()
        try:   # 서버는 본문을 읽지 않고 413 을 보낸 뒤 끊는다(보내는 쪽에서는 끊김으로 보일 수 있다)
            self.assertEqual(self.send("/api/vpns", big)[0], 413)
        except (ConnectionError, http.client.HTTPException):
            pass
        self.assertFalse((Path(self.tmp) / "ovpn" / "office").exists())
        self.assertEqual(self.send("/api/profiles/vpn-a/on", b"x" * 2000)[0], 413)   # 켜기·끄기의 본문 한도는 그대로

    def test_upload_ports_skip_used_ones_and_bind_loopback_only(self):
        self.use_checker()
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        self.addCleanup(busy.close)
        self.assertFalse(gui.vpnstore.port_free(busy.getsockname()[1]))
        self.manager.store.port_free = lambda port: port not in (1084, 1085)   # 다른 프로그램이 쓰는 포트
        self.assertIn("127.0.0.1:1086", self.upload("one")[1]["message"])
        self.manager.store.port_free = lambda port: True
        self.assertIn("127.0.0.1:1084", self.upload("two")[1]["message"])
        gen = json.loads((Path(self.tmp) / "compose.generated.yml").read_text())["services"]
        for svc in gen.values():
            self.assertRegex(svc["ports"][0], r"^127\.0\.0\.1:10[0-9]{2}:1080$")
            self.assertEqual(len(svc["ports"]), 1)
        self.manager.store.port_free = lambda port: False
        self.assertIn("빈 포트가 없습니다", self.upload("three")[1]["message"])
        self.assertFalse((Path(self.tmp) / "ovpn" / "three").exists())

    def test_upload_is_rolled_back_when_compose_refuses_it(self):
        self.use_checker()
        self.state["config_error"] = True
        self.save()
        # 기본 파일만 읽을 때는 되고, 생성 파일을 함께 읽으면 실패하는 상황
        status, data = self.upload()
        self.assertEqual((status, data["ok"]), (409, False))
        self.assertIn("되돌렸습니다", data["message"])
        for left in ("ovpn/office", "vpns.json.tmp", "compose.generated.yml"):
            self.assertFalse((Path(self.tmp) / left).exists(), left)
        self.assertEqual(json.loads((Path(self.tmp) / "vpns.json").read_text()), {"vpns": []})

    def test_delete_needs_confirmation_and_only_removes_uploaded_vpns(self):
        self.use_checker()
        self.assertEqual(self.upload(ovpn=FAKE_OVPN + "auth-user-pass\n", username="u", password=PW_MARK)[0], 200)
        self.assertEqual(self.upload("keep")[0], 200)
        folder = Path(self.tmp) / "ovpn" / "office"
        for body in ({}, {"confirm": ""}, {"confirm": "keep"}, {"confirm": True}, {"confirm": ["office"]}):
            status, data = self.send("/api/vpns/office/delete", body)
            self.assertEqual((status, data["ok"]), (409, False), body)
            self.assertIn("삭제 확인", data["message"])
        self.assertTrue(folder.is_dir())
        self.assertEqual([c for c in self.changing_calls() if c[-2:-1] != ["--no-build"]], [])
        # compose 에 적힌 VPN, 없는 이름, 이상한 이름은 지우지 않는다
        for name in ("vpn-a", "nope"):
            status, data = self.send(f"/api/vpns/{name}/delete", {"confirm": name})
            self.assertEqual(status, 409, name)
            self.assertIn("웹에서 올린 VPN 만", data["message"])
        for path in ("/api/vpns/../delete", "/api/vpns/A/delete", "/api/vpns/a%2Fb/delete", "/api/vpns/office/delete?confirm=office"):
            self.assertIn(self.send(path, {"confirm": "office"})[0], (400, 404), path)
        self.assertTrue((Path(self.tmp) / "ovpn" / "vpn-a" / "client.ovpn").exists())
        # Docker 가 꺼져 있으면 컨테이너를 정리할 수 없으므로 지우지 않는다
        self.state["daemon"] = False
        self.save()
        status, data = self.send("/api/vpns/office/delete", {"confirm": "office"})
        self.assertEqual(status, 409)
        self.assertTrue(folder.is_dir())
        self.state["daemon"] = True
        self.save()
        # 확인하면: 컨테이너 정지·삭제 → 등록부에서 빼기 → 폴더 삭제
        status, data = self.send("/api/vpns/office/delete", {"confirm": "office"})
        self.assertEqual((status, data), (200, {"ok": True, "message": "지웠습니다."}))
        self.assertEqual(self.calls()[-2], ["compose", "-f", "docker-compose.yml", "-f", "compose.generated.yml", "rm", "--stop", "--force", "office"])
        self.assertFalse(folder.exists())
        self.assertEqual([e["name"] for e in json.loads((Path(self.tmp) / "vpns.json").read_text())["vpns"]], ["keep"])
        self.assertEqual(sorted(self.by_id(self.overview())), ["keep", "vpn-a", "vpn-b", "vpn-c"])
        # 마지막 것을 지우면 생성 파일도 없어지고, 기본 compose 만 쓴다
        self.assertEqual(self.send("/api/vpns/keep/delete", {"confirm": "keep"})[0], 200)
        self.assertFalse((Path(self.tmp) / "compose.generated.yml").exists())
        self.assertEqual(self.post("/api/profiles/vpn-a/on")[0], 200)
        self.assertEqual(self.calls()[-1], ["compose", "up", "-d", "--no-build", "vpn-a"])
        self.assertTrue((Path(self.tmp) / "ovpn" / "vpn-a" / "auth.txt").exists())   # 다른 VPN 의 파일은 그대로

    def test_delete_does_not_follow_a_link_out_of_the_ovpn_folder(self):
        self.use_checker()
        self.assertEqual(self.upload()[0], 200)
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        shutil.rmtree(Path(self.tmp) / "ovpn" / "office")
        os.symlink(outside, Path(self.tmp) / "ovpn" / "office")
        self.assertEqual(self.send("/api/vpns/office/delete", {"confirm": "office"})[0], 200)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertFalse((Path(self.tmp) / "ovpn" / "office").is_symlink())

    def test_registry_file_is_not_trusted(self):
        self.use_checker()
        bad = {"vpns": [{"name": "../../etc", "port": 1084}, {"name": "ok", "port": 80}, {"name": "ok2", "port": "1084"},
                        {"name": "ok3", "port": True}, {"name": "fine", "port": 1090, "folder": "/etc"}, "x", {"name": "fine", "port": 1091}]}
        (Path(self.tmp) / "vpns.json").write_text(json.dumps(bad))
        self.assertEqual(self.manager.store.entries(), [{"name": "fine", "port": 1090}])
        self.assertEqual(self.manager.store.service({"name": "fine", "port": 1090})["volumes"], ["./ovpn/fine:/vpn:ro"])   # 폴더는 이름에서만 만든다
        for junk in ("{", "[]", '{"vpns": 3}'):
            (Path(self.tmp) / "vpns.json").write_text(junk)
            self.assertEqual(self.manager.store.entries(), [])

    def test_generated_service_matches_the_compose_template(self):
        """생성 서비스의 권한·격리 값이 docker-compose.yml 의 x-vpn 블록과 같다(진짜 docker compose config 로 비교)."""
        docker = shutil.which("docker") or "/Users/%s/.docker/bin/docker" % os.environ.get("USER", "")
        if not os.path.exists(docker) or gui.Docker(binary=docker, cwd=self.tmp).run(["compose", "version"])[0] != 0:
            self.skipTest("docker compose 가 없다")
        work = Path(self.tmp) / "real"
        work.mkdir()
        shutil.copy(REPO / "docker-compose.yml", work / "docker-compose.yml")
        store = gui.vpnstore.Store(work)
        store.save([{"name": "office", "port": 1084}])
        rc, out, err = gui.Docker(binary=docker, cwd=work).run(["compose", *store.compose_files(), "config", "--format", "json"])
        self.assertEqual(rc, 0, err)
        svcs = json.loads(out)["services"]
        mine, base = svcs["office"], svcs["vpn-a"]
        differ = {"container_name", "environment", "ports", "volumes"}
        self.assertEqual({k: v for k, v in mine.items() if k not in differ}, {k: v for k, v in base.items() if k not in differ})
        self.assertEqual(mine["ports"], [{**base["ports"][0], "published": "1084"}])
        self.assertEqual(mine["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual((mine["volumes"][0]["target"], mine["volumes"][0].get("read_only")), ("/vpn", True))
        self.assertEqual(mine["volumes"][0]["source"], str((work / "ovpn" / "office").resolve()))

    def test_repo_files(self):
        self.assertIn("!rootfs/", (REPO / ".dockerignore").read_text())  # 화면 코드는 이미지에 들어가지 않는다
        src = (REPO / "gui" / "server.py").read_text()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)
        # 파일을 여는 곳은 ovpn 에서 서버 주소를 읽는 한 곳뿐이다(auth 파일은 열지 않는다)
        self.assertEqual(src.count("open("), 1)
        self.assertNotIn("auth.txt", src.split('"""', 2)[2])
        self.assertEqual(src.count("read_bytes"), 1)  # 정적 파일 한 곳뿐
        # 등록부 모듈: 셸을 거치지 않고, 읽는 파일은 등록부(vpns.json) 하나뿐이다(ovpn·auth 는 쓰기만 한다)
        store = (REPO / "gui" / "vpnstore.py").read_text()
        for bad in ("shell=True", "os.system", "read_bytes", "print("):
            self.assertNotIn(bad, store)
        self.assertEqual(store.count("read_text"), 1)
        self.assertEqual(len(re.findall(r"(?<![.\w])open\(", store)), 0)   # 파일은 os.open 으로만 연다
        self.assertEqual((store.count("os.open("), store.count("os.O_WRONLY")), (2, 2))   # 둘 다 쓰기 전용
        self.assertNotIn("$\")", store)   # 이름·비밀번호 검사는 fullmatch 로 한다
        ignore = (REPO / ".gitignore").read_text()
        for name in ("vpns.json", "compose.generated.yml"):
            self.assertIn(name, ignore)


if __name__ == "__main__":
    unittest.main(verbosity=2)
