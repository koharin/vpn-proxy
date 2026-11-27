#!/usr/bin/env python3
"""VPN 등록부를 명령줄에서 다룬다. 웹 화면으로 올릴 수 없는 것(인증서가 따로 있는 ovpn)이나 예전 방식에서 옮길 때 쓴다.

  python3 gui/vpnctl.py list
  python3 gui/vpnctl.py import <이름> [--port 1081] [--dns 10.0.0.2,10.0.0.3]
        이미 있는 ovpn/<이름>/ 폴더를 등록한다. 포트를 적지 않으면 1081 부터 빈 번호.
  python3 gui/vpnctl.py set <이름> [--port N] [--dns 주소,…|--dns ""]
        포트·DNS 를 바꾼다. 컨테이너를 껐다 켜야 적용된다.
  python3 gui/vpnctl.py forget <이름>
        등록만 지운다(폴더·컨테이너는 그대로).

ovpn 과 아이디·비밀번호 파일의 내용은 읽어 찍지 않는다. 이름·포트·DNS·검사 사유만 찍는다.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vpnstore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None, root=ROOT, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="vpnctl.py", description="vpn-proxy 등록부")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("import", "set"):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--port", type=int)
        p.add_argument("--dns")
    sub.add_parser("forget").add_argument("name")
    args = ap.parse_args(argv)
    store = vpnstore.Store(root)
    try:
        if args.cmd == "list":
            for e in store.entries():
                print(f"{e['name']}\t127.0.0.1:{e['port']}\t{vpnstore.OVPN_DIR}/{e['name']}"
                      + ("\tDNS " + ",".join(e["dns"]) if e["dns"] else "") + ("\t(가져온 폴더)" if e["imported"] else ""), file=out)
            return 0
        if args.cmd == "import":
            made = store.import_folder(args.name, port=args.port, dns=args.dns)
            print(f"등록했습니다: {made['name']} → SOCKS5 127.0.0.1:{made['port']}. 웹 화면에서 켜거나 "
                  f"docker compose -f {vpnstore.BASE_COMPOSE} -f {vpnstore.GENERATED} up -d {made['name']}", file=out)
            return 0
        if args.cmd == "set":
            made = store.update(args.name, port=args.port, dns=args.dns)
            print(f"바꿨습니다: {made['name']} → 127.0.0.1:{made['port']}" + (" DNS " + ",".join(made["dns"]) if made["dns"] else "")
                  + ". 껐다 켜면 적용됩니다.", file=out)
            return 0
        if not store.forget(args.name):
            raise vpnstore.Rejected("등록되지 않은 이름입니다.")
        print(f"등록을 지웠습니다: {args.name} (폴더와 컨테이너는 그대로입니다)", file=out)
        return 0
    except vpnstore.Rejected as e:
        print("안 됨: " + e.message, file=out)
        for r in e.reasons:
            print("  " + r, file=out)
        return 1
    except OSError as e:
        print(f"안 됨: 파일을 쓰지 못했습니다({e.strerror}).", file=out)
        return 1


if __name__ == "__main__":
    sys.exit(main())
