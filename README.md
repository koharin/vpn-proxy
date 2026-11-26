# vpn-proxy

내부 대역이 서로 겹치는 OpenVPN 3개를 Mac 에서 **동시에** 쓴다.
Mac 에는 VPN 을 켜지 않는다. VPN마다 컨테이너 하나(OpenVPN 클라이언트 + SOCKS5 프록시)를 띄우고,
어느 VPN 으로 갈지는 **로컬 포트**로 고른다.

| 로컬 주소 | VPN | 컨테이너 | ovpn 폴더 |
|---|---|---|---|
| `127.0.0.1:1081` | vpn-a | `vpn-proxy-a` | `ovpn/vpn-a/` |
| `127.0.0.1:1082` | vpn-b | `vpn-proxy-b` | `ovpn/vpn-b/` |
| `127.0.0.1:1083` | vpn-c | `vpn-proxy-c` | `ovpn/vpn-c/` |

## 구성

```
 Mac (VPN 안 켬, 라우팅 그대로)
 ┌──────────────────────────────────────────────────────────────────┐
 │  ssh / 브라우저 / curl                                           │
 │     │ SOCKS5                                                     │
 │     ├─ 127.0.0.1:1081 ─▶ [vpn-proxy-a] sockd ─▶ tun0 ═══▶ VPN A  │──▶ 10.0.0.0/16 (A)
 │     ├─ 127.0.0.1:1082 ─▶ [vpn-proxy-b] sockd ─▶ tun0 ═══▶ VPN B  │──▶ 10.0.0.0/16 (B)
 │     └─ 127.0.0.1:1083 ─▶ [vpn-proxy-c] sockd ─▶ tun0 ═══▶ VPN C  │──▶ 10.0.0.0/16 (C)
 └──────────────────────────────────────────────────────────────────┘
```

컨테이너마다 네트워크가 따로라서 세 VPN 의 대역이 같아도 부딪히지 않는다.
같은 `10.0.0.10` 이라도 1081 로 가면 A 의 서버, 1082 로 가면 B 의 서버다.

컨테이너 안의 안전장치:

- **방화벽**: 나가는 길은 터널(`tun0`)과 VPN 서버 주소뿐이다. 터널이 없으면 아무 데도 못 나간다.
- **kill-switch**: 터널이 내려가면 SOCKS5 를 끄고, 다시 올라오면 켠다. OpenVPN 이 죽으면 컨테이너가 끝나고 다시 뜬다.
- **DNS**: VPN 이 알려 준 DNS 만 쓴다. 없으면 이름 해석을 하지 않는다(터널 밖으로 이름이 새지 않게).
- **ovpn 검사**: 허용 목록에 있는 지시만 받는다. 그 밖의 지시(`route-up`, `plugin` 등)가 있으면 시작하지 않는다.
- **스크립트 실행 안 함**: OpenVPN 을 `--script-security 1` 로 띄운다. 검사와 별개로 한 겹 더 막는다.
- **포트**: Mac 의 `127.0.0.1` 에만 연다. 같은 네트워크의 다른 기기는 붙을 수 없다. SOCKS5 에 비밀번호는 없다.

## 준비물

- Docker Desktop 또는 OrbStack (`docker compose` 사용)
- VPN별 `.ovpn` 파일. 이 저장소에는 들어 있지 않고, 넣어도 git 에 올라가지 않는다(`.gitignore`).
  **믿을 수 있는 출처(회사에서 직접 받은 것)의 ovpn 만 넣는다.** ovpn 은 VPN 서버 주소와 경로를 정하는
  파일이라, 남이 만든 ovpn 을 넣으면 그쪽 서버로 연결된다. 아래 "ovpn 검사"는 명령 실행을 막을 뿐이다.

## 설치와 실행 (3단계)

**1. ovpn 을 넣는다.** VPN 하나에 폴더 하나, 폴더 안에 `.ovpn` 은 1개만.

```sh
cp ~/Downloads/회사A.ovpn ovpn/vpn-a/client.ovpn
cp ~/Downloads/회사B.ovpn ovpn/vpn-b/client.ovpn
cp ~/Downloads/회사C.ovpn ovpn/vpn-c/client.ovpn
```

- 파일 이름은 `.ovpn` 으로 끝나면 무엇이든 된다.
- ovpn 이 인증서 파일을 따로 가리키면(`ca ca.crt` 등) 같은 폴더에 함께 넣는다.
- 아이디·비밀번호를 묻는 VPN(`auth-user-pass`)은 같은 폴더에 `auth.txt` 를 만든다.
  첫 줄 아이디, 둘째 줄 비밀번호. `chmod 600 ovpn/vpn-a/auth.txt`. 이 파일도 git 에 올라가지 않는다.

**2. 띄운다.**

```sh
docker compose up -d --build
```

포트나 이름을 바꾸려면 먼저 `cp .env.example .env` 하고 고친다(안 해도 기본값으로 돈다).

**3. 확인한다.**

```sh
docker compose ps                       # 세 개 모두 (healthy) 면 연결된 것
docker compose logs -f vpn-a            # "SOCKS5 시작" 이 보이면 준비 끝
curl --socks5-hostname 127.0.0.1:1081 http://10.0.0.10/    # vpn-a 안의 서버 (주소는 예시)
```

멈추기: `docker compose down`. 하나만 다시: `docker compose restart vpn-b`.

## 웹 화면

켜고 끄기와 상태 확인을 브라우저에서 한다. Mac 에서 도는 작은 서버가 이 폴더의 `docker compose` 를 대신 부른다.
필요한 것은 Mac 에 이미 있는 `python3` 뿐이다(설치할 것 없음).

```sh
docker compose build                 # 처음 한 번(화면의 '켜기'는 이미지를 빌드하지 않는다)
python3 gui/server.py                # http://127.0.0.1:8030   끝내기: Ctrl+C
VPN_GUI_PORT=8031 python3 gui/server.py
```

화면에 보이는 것: compose 에 정의된 VPN 목록, 켜기·끄기 버튼, 상태(연결됨 / 연결 중 / 실패와 사유 / 꺼짐),
SOCKS5 주소·포트, ovpn 폴더와 `.ovpn` 파일 수, 최근 로그 40줄.

- 켜기 = `docker compose up -d --no-build <서비스>`, 끄기 = `docker compose stop <서비스>`.
- 실패한 VPN 은 컨테이너가 10초마다 다시 시도한다. 멈추려면 끄기를 누른다.
- ovpn 은 올릴 수 없다. `ovpn/<이름>/` 폴더에 직접 넣는다. ovpn 검사는 컨테이너가 시작할 때 그대로 돌고,
  거부되면 화면에 '실패'와 사유가 뜬다.
- VPN 을 늘리려면 compose 에 서비스를 추가한다("VPN 추가"). 화면은 compose 를 읽어 목록을 만든다.

지키는 것:

- `127.0.0.1` 에만 연다. 주소는 바꿀 수 없고 포트만 바꾼다. 로그인은 없다 — 이 Mac 의 사용자만 쓴다고 본다.
- 상태를 바꾸는 요청은 POST 뿐이고, 이 화면 자신이 보낸 것(Origin·Host 가 `127.0.0.1`/`localhost` 의 그 포트)만 받는다.
  다른 웹사이트가 브라우저를 시켜 VPN 을 켜고 끌 수 없다.
- 다룰 수 있는 대상은 compose 에 정의된 서비스뿐이다.
- ovpn·`auth.txt` 의 내용은 읽지 않는다. 로그는 비밀번호·토큰·키처럼 보이는 값을 가리고 보여 준다.

시험: `python3 tests/gui_test.py` (진짜 docker 를 부르지 않는다).

## 쓰는 법

### SSH

```sh
ssh -o ProxyCommand='nc -X 5 -x 127.0.0.1:1081 %h %p' ubuntu@10.0.0.10
```

매번 치지 않으려면 [`examples/ssh_config`](examples/ssh_config) 를 `~/.ssh/config` 에 붙인다.
`ssh web-a`, `ssh web-b` 처럼 별칭이 VPN 을 정한다. `scp`, `rsync`, `git`(ssh 주소)도 같은 설정을 따른다.

### 브라우저

Chrome 을 VPN마다 다른 프로필로 띄우거나 FoxyProxy 로 바꿔 쓴다. [`examples/browser.md`](examples/browser.md).

### curl 등 명령줄 도구

```sh
curl --socks5-hostname 127.0.0.1:1082 https://intra.example.internal/
ALL_PROXY=socks5h://127.0.0.1:1082 curl https://intra.example.internal/
```

SOCKS 옵션이 없는 프로그램은 proxychains 로 감싼다. [`examples/proxychains-a.conf`](examples/proxychains-a.conf).

### 내부 DNS 이름

`socks5h://`(curl 은 `--socks5-hostname`)로 쓴다. `h` 가 붙으면 이름을 Mac 이 아니라 컨테이너가,
즉 **VPN 안의 DNS** 로 푼다. `socks5://` 로 쓰면 Mac 이 먼저 이름을 풀려다 실패한다.
(브라우저는 `socks5://` 로 적어도 프록시 쪽에서 푼다.)

VPN 이 DNS 를 알려 주지 않으면 로그에 경고가 뜨고 IP 로만 접속된다. 이때는 `.env` 의
`VPN_A_DNS=10.0.0.2` 처럼 내부 DNS 주소를 적고 `docker compose up -d` 로 다시 띄운다.

### RDP · DB · 그 밖의 TCP 프로그램

SOCKS 를 모르는 프로그램은 VPN 안의 SSH 서버를 거쳐 로컬 포트를 연다.

```sh
# RDP: 클라이언트에서 127.0.0.1:13389 로 접속
ssh -N -L 127.0.0.1:13389:10.0.0.30:3389 web-a

# PostgreSQL
ssh -N -L 127.0.0.1:15432:10.0.1.20:5432 web-b
psql -h 127.0.0.1 -p 15432 -U app appdb
```

`web-a`, `web-b` 는 `examples/ssh_config` 의 별칭이다(그 VPN 안에서 SSH 로 들어갈 수 있는 서버).
거칠 SSH 서버가 없으면 그 프로그램을 proxychains 로 감싸서 실행한다.

### ping · UDP · traceroute

SOCKS 는 TCP 만 지나간다. ping 과 UDP 는 컨테이너 안에서 실행한다.

```sh
docker exec vpn-proxy-a ping -c 3 10.0.0.10
docker exec vpn-proxy-a nc -vz 10.0.0.10 22          # TCP 포트 확인
docker exec vpn-proxy-a dig +short db.internal        # VPN DNS 로 이름 풀기
docker exec vpn-proxy-a ip route                      # VPN 이 내려 준 경로
```

컨테이너에는 임의 패킷을 만드는 권한(NET_RAW)을 주지 않았다. ping 은 되지만 `traceroute`, `nmap -sS`
처럼 직접 패킷을 만드는 도구는 안 된다. 경로는 `ip route` 로 본다.

## VPN 추가

1. `ovpn/vpn-d/` 폴더를 만들고 `.ovpn` 을 넣는다.
2. `docker-compose.yml` 맨 아래에 블록을 하나 더 붙인다. 이름·폴더·포트 세 곳만 다르다.

   ```yaml
     vpn-d:
       <<: *vpn
       container_name: vpn-proxy-d
       environment:
         VPN_NAME: vpn-d
       volumes:
         - ./ovpn/vpn-d:/vpn:ro
       ports:
         - "127.0.0.1:1084:1080"     # 반드시 127.0.0.1: 로 시작
   ```

3. `docker compose up -d`.

## 설정 값 (`.env`)

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `VPN_A_NAME` | `vpn-a` | 로그에 찍히는 이름 |
| `VPN_A_DIR` | `./ovpn/vpn-a` | ovpn 폴더(읽기 전용으로 붙는다) |
| `VPN_A_PORT` | `1081` | Mac 의 `127.0.0.1` 포트 |
| `VPN_A_AUTH_FILE` | (없음) | 아이디·비밀번호 파일 이름. 비우면 `auth.txt` 가 있을 때 그것을 쓴다 |
| `VPN_A_DNS` | (없음) | VPN 이 DNS 를 안 알려 줄 때 쓸 내부 DNS. 쉼표로 여러 개 |
| `VPN_CONNECT_TIMEOUT` | `60` | 연결을 기다리는 초 |

`B`, `C` 도 같다. 비밀번호는 `.env` 에 적지 않는다.

## 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| 로그에 `ovpn 파일이 없습니다` | `ovpn/vpn-a/` 에 `.ovpn` 이 없다. 넣으면 10초쯤 뒤 저절로 다시 시도한다 |
| `.ovpn 파일이 2 개입니다` | 폴더에 1개만 남긴다 |
| `아이디·비밀번호가 필요합니다` | 같은 폴더에 `auth.txt` 를 만든다 |
| `ovpn 에 받지 않는 내용이 있습니다` | 로그에 찍힌 줄 번호의 지시·글자를 ovpn 에서 지운다(한글 주석 포함). 아래 "ovpn 검사" 참고 |
| `/etc/resolv.conf 를 쓸 수 없습니다` | 컨테이너를 읽기 전용(`read_only`)으로 띄우지 않는다 |
| `OpenVPN 이 연결 전에 끝났습니다` | 바로 위의 OpenVPN 로그를 본다. 인증서 경로, 만료, 비밀번호 오류가 흔하다 |
| `60초 안에 VPN 이 연결되지 않았습니다` | 서버 주소·포트, 회사 방화벽을 확인한다. 느린 VPN 은 `VPN_CONNECT_TIMEOUT` 을 늘린다 |
| `VPN 서버 주소를 하나도 찾지 못했습니다` | ovpn 의 `remote` 이름이 Mac 에서 풀리는지 본다(`dig <이름>`) |
| `/dev/net/tun 이 없습니다` | Docker Desktop·OrbStack 을 최신으로 올린다. compose 의 `devices`, `cap_add` 를 지우지 않았는지 본다 |
| curl 이 바로 끊긴다 | 터널이 아직 안 올라왔다. `docker compose ps` 가 healthy 인지, 로그에 `SOCKS5 시작` 이 있는지 본다 |
| IP 는 되는데 이름이 안 된다 | `socks5h` 로 썼는지, 로그에 DNS 경고가 있는지 본다. 있으면 `VPN_A_DNS` 를 적는다 |
| 인터넷 사이트가 안 열린다 | VPN 이 내부 대역만 받는 설정이면 정상이다. 터널 밖으로는 내보내지 않는다 |
| 포트가 이미 쓰이고 있다 | `.env` 의 `VPN_A_PORT` 를 바꾼다 |
| 연결은 되는데 특정 대역만 안 된다 | `docker exec vpn-proxy-a ip route` 로 VPN 이 그 대역 경로를 줬는지 본다. VPN 대역이 Docker 내부 대역(보통 `172.x`)과 겹치면 Docker 설정에서 기본 주소 대역을 바꾼다 |
| ovpn 의 `user nobody` 등으로 재연결이 실패한다 | 권한을 내린 뒤에는 경로를 다시 못 건다. `docker compose restart vpn-a` 로 다시 띄운다 |

상태를 한눈에:

```sh
docker compose ps
docker compose logs --tail 30 vpn-a
docker exec vpn-proxy-a iptables -S          # 방화벽 규칙
docker exec vpn-proxy-a ip -4 addr show tun0 # 터널 주소
```

## ovpn 검사

시작할 때 ovpn 을 읽어 **허용 목록에 있는 지시만** 받는다. 목록에 없는 것이 한 줄이라도 있으면
줄 번호를 알리고 멈춘다. ovpn 을 만든 쪽이 컨테이너 안에서 명령을 실행하거나, 라이브러리를 불러오거나,
파일을 쓰거나, 방화벽을 끄지 못하게 하기 위함이다. 검사는 `rootfs/check-ovpn.sh` 가 한다.

**1. 글자** — 인쇄 가능한 ASCII, 탭, 줄 끝의 CR(윈도우 줄 끝)만 받는다.
세로 탭, 폼 피드, 줄 중간의 CR, BOM, NUL, **한글 주석**이 있으면 멈춘다. 255자를 넘는 줄도 멈춘다.
OpenVPN 은 이런 글자를 공백처럼 건너뛰기도 해서, 검사와 OpenVPN 이 같은 줄을 다르게 읽는 틈이 생기기 때문이다.

**2. 지시** — 줄의 첫 단어가 소문자·숫자·`-` 로만 되어 있고 아래 목록에 있어야 한다.

| 종류 | 받는 지시 |
|---|---|
| 접속 | `client` `pull` `tls-client` `dev` `dev-type` `proto` `remote` `remote-random` `remote-random-hostname` `resolv-retry` `nobind` `port` `lport` `rport` `float` |
| 유지·재접속 | `persist-key` `persist-tun` `persist-local-ip` `persist-remote-ip` `connect-retry` `connect-retry-max` `connect-timeout` `server-poll-timeout` `explicit-exit-notify` `keepalive` `ping` `ping-restart` `ping-exit` `ping-timer-rem` `inactive` `reneg-sec` `reneg-bytes` `reneg-pkts` `hand-window` `tran-window` `tls-timeout` `tls-exit` `single-session` |
| 암호·인증 | `cipher` `data-ciphers` `data-ciphers-fallback` `ncp-ciphers` `ncp-disable` `auth` `auth-nocache` `auth-retry` `auth-user-pass` `tls-version-min` `tls-version-max` `tls-cipher` `tls-ciphersuites` `tls-groups` `tls-cert-profile` `tls-auth` `tls-crypt` `tls-crypt-v2` `key-direction` `remote-cert-tls` `remote-cert-ku` `remote-cert-eku` `verify-x509-name` `ns-cert-type` `peer-fingerprint` `ca` `cert` `key` `extra-certs` `pkcs12` `dh` `crl-verify` |
| 압축·크기 | `comp-lzo` `compress` `allow-compression` `comp-noadapt` `mssfix` `fragment` `tun-mtu` `tun-mtu-extra` `link-mtu` `mtu-disc` `mtu-test` `sndbuf` `rcvbuf` `txqueuelen` `fast-io` |
| 경로·주소 | `route` `route-metric` `route-delay` `route-nopull` `route-gateway` `redirect-gateway` `redirect-private` `allow-pull-fqdn` `pull-filter` `ifconfig` `ifconfig-nowarn` `topology` `dhcp-option` `block-outside-dns` |
| 그 밖 | `verb` `mute` `mute-replay-warnings` `replay-window` `disable-occ` `ignore-unknown-option` `push-peer-info` `user` `group` |
| `setenv` | `setenv CLIENT_CERT·FRIENDLY_NAME·GENERIC_CONFIG·FORWARD_COMPATIBLE·PUSH_PEER_INFO …` 과 `setenv opt <위 목록의 지시>` 만. `setenv opt` 뒤의 지시도 같은 기준으로 검사한다 |

목록에 없으면 받지 않는다. 예: `up` `down` `route-up` `route-pre-down` `ipchange` `tls-verify`
`auth-user-pass-verify` `client-connect` `learn-address` `tls-crypt-v2-verify`(명령 실행),
`plugin` `providers` `engine` `pkcs11-providers`(라이브러리), `script-security` `config` `management`
`setenv PATH`(실행 환경), `log` `status` `writepid`(파일 쓰기), `http-proxy` `socks-proxy`(이 구성에서 쓰지 않음).
`dhcp-option DNS 127.x` 처럼 DNS 를 컨테이너 자신으로 돌리는 값도 받지 않는다.

**3. 블록** — `<ca>` `<cert>` `<key>` `<extra-certs>` `<dh>` `<pkcs12>` `<secret>` `<tls-auth>` `<tls-crypt>`
`<tls-crypt-v2>` `<crl-verify>` `<auth-user-pass>` `<peer-fingerprint>` 와 `<connection>` 만 받는다.
인증서 블록의 안쪽은 지시로 보지 않는다. `<connection>` 안쪽은 지시로 검사한다.
닫는 표시는 OpenVPN 과 같은 기준으로 본다: 앞 공백을 뺀 줄이 `</이름>` 으로 시작하면 닫는 것이고,
그 줄에 다른 글자가 더 있으면(`</ca>x`) 멈춘다.

**4. 파일 경로** — `ca` `cert` `key` `extra-certs` `pkcs12` `dh` `crl-verify` `tls-auth` `tls-crypt`
`tls-crypt-v2` `auth-user-pass` 뒤의 파일 경로는 ovpn 폴더 안의 상대 경로만 받는다(`ca ca.crt`, `cert certs/client.crt`).
`/` 로 시작하거나 `..` 이 들어 있거나 따옴표·역슬래시·공백이 든 경로는 멈춘다. 컨테이너 안의 다른 파일을
읽어 서버로 보내게 하는 것을 막기 위함이다. 인증서를 ovpn 안에 `<ca>…</ca>` 블록으로 넣는 것과,
경로 없는 `auth-user-pass`(같은 폴더의 `auth.txt` 사용)는 그대로 된다.

**회사에서 받은 ovpn 이 거부되면** 로그에 찍힌 줄을 지운다. 흔한 경우:

- `up /etc/openvpn/update-resolv-conf`, `down …`, `script-security 2` → 지운다. DNS 는 이 구성이 대신 설정한다.
- `log`, `status`, `writepid` → 지운다. 로그는 `docker compose logs` 로 본다.
- 한글 주석 → 지우거나 영어로 바꾼다.
- `user nobody`, `group nogroup` → 그대로 둬도 된다. OpenVPN 에 넘기는 사본에서 자동으로 빼고 로그에
  `줄 N: user/group 은 컨테이너 안에서 쓰지 않아 뺐습니다` 를 찍는다(원본 파일은 바꾸지 않는다). 이 컨테이너의
  root 에는 다른 사용자 프로세스에 신호를 보낼 권한(CAP_KILL)이 없어, OpenVPN 이 `nobody` 로 내려가면
  살아 있는지 확인도 종료도 할 수 없기 때문이다.
- `ca /etc/openvpn/ca.crt` 같은 절대 경로 → 그 파일을 ovpn 과 같은 폴더에 두고 `ca ca.crt` 로 바꾼다.
- 꼭 필요한 지시가 목록에 없으면 `rootfs/check-ovpn.sh` 의 목록에 더하고 다시 빌드한다(더하기 전에 그 지시가
  명령 실행·파일 쓰기를 하지 않는지 OpenVPN 문서로 확인한다).

**검사가 뚫려도 스크립트는 실행되지 않는다.** OpenVPN 을 `--script-security 1` 로 띄운다. 이 값에서는
OpenVPN 이 스크립트를 부르지 못한다(`ip` 같은 내장 동작만). 터널 상태와 DNS 는 훅 스크립트 없이
entrypoint 가 OpenVPN 의 로그와 터널 장치를 보고 처리한다. 단, `plugin`·`providers` 처럼 라이브러리를
불러오는 지시는 이 값과 관계없으므로 검사가 막는다.

## 상태 읽기

컨테이너마다 `/run/vpn` 에 상태를 남긴다(root 만 쓴다). 화면이나 스크립트에서 읽기 쉽게 두 가지로 둔다.

```sh
docker exec vpn-proxy-a cat /run/vpn/state          # 한 단어
docker exec vpn-proxy-a cat /run/vpn/status.json    # 한 줄 JSON
```

| `state` | 뜻 | SOCKS5 |
|---|---|---|
| `starting` | 설정·ovpn 검사, 방화벽 적용 중 | 꺼짐 |
| `connecting` | VPN 서버에 접속 중 | 꺼짐 |
| `up` | 터널 연결됨 | 켜짐 |
| `reconnecting` | 터널이 내려가 다시 접속 중 | 꺼짐 |
| `stopped` | 오류나 종료 요청으로 끝나는 중(`message` 에 이유) | 꺼짐 |

```json
{"name":"vpn-a","state":"up","since":"2026-11-25T06:55:12Z","gen":1,"dns":["10.0.0.2"],"socks_port":1080,"message":"터널 연결됨"}
```

`gen` 은 터널이 올라온 횟수다. 컨테이너가 떠 있지 않으면 이 파일도 없으므로 `docker compose ps` 를 함께 본다.
`docker compose ps` 의 healthy 는 `state` 가 `up` 이고 SOCKS 포트가 열려 있다는 뜻이다.

상태는 OpenVPN 로그의 줄 전체 모양으로 판단한다. 시각 뒤가 정확히 `Initialization Sequence Completed` 인 줄만
연결됨으로, `SIG…[…] received, process restarting|exiting` 인 줄만 내려감으로 본다. 서버 인증서 이름처럼
상대가 정한 글이 로그 줄 중간에 찍혀도 상태는 바뀌지 않는다.

## 점검 스크립트

실제 VPN 없이 확인할 수 있는 것을 확인한다(설정, 오류 안내, ovpn 검사와 우회 시도, 스크립트 미실행, 방화벽, 127.0.0.1 바인드, 권한, 재연결, kill-switch).
터널 시험은 같은 이미지로 상대 컨테이너를 하나 띄워 암호 없는 1:1 터널을 맺는 방식이다(비밀값 없음, 이 Mac 안에서만 돈다).

```sh
docker compose build && sh tests/smoke.sh
```

`vpn-proxy-test-*` 이름의 컨테이너와 네트워크만 만들고 끝나면 지운다. 실제 VPN 서버 접속은 이 스크립트로 확인되지 않는다.

## 파일

```
docker-compose.yml     VPN 3개 서비스(늘리기 쉽게 공통 블록 사용)
Dockerfile             alpine + openvpn + dante(SOCKS5) + iptables
rootfs/entrypoint.sh   설정 확인 → ovpn 검사 → 방화벽 → OpenVPN → 터널 확인 → SOCKS5, 감시
rootfs/check-ovpn.sh   ovpn 검사(허용 목록)
rootfs/vpn-lib.sh      상태 기록, DNS 적용, OpenVPN 로그 따라가기
rootfs/healthcheck.sh  터널 + SOCKS 포트 확인
.env.example           포트·폴더·이름
ovpn/vpn-{a,b,c}/      ovpn 을 넣는 곳(내용은 git 제외)
examples/              ssh_config, browser.md, proxychains-{a,b,c}.conf
tests/smoke.sh         점검 스크립트
```

## 알아 둘 점

- SOCKS5 에는 비밀번호가 없다. Mac 에 로그인한 사용자와 프로그램은 누구나 이 포트를 쓸 수 있다.
  포트를 `0.0.0.0` 으로 바꾸지 않는다.
- 이 구성의 Docker 네트워크(`vpn-proxy_default`)에 **다른 컨테이너를 붙이지 않는다.** 같은 네트워크의
  컨테이너는 비밀번호 없이 이 프록시를 쓸 수 있다. 기본 설정에는 이 구성의 컨테이너만 들어 있다.
- SOCKS 로 컨테이너 자신(`127.0.0.0/8`)에는 접속할 수 없다.
- 베이스 이미지는 정확한 버전과 다이제스트로 고정돼 있다(`Dockerfile`). 보안 갱신을 받으려면 그 줄을 올리고 다시 빌드한다.
- IPv6 는 컨테이너에서 꺼 둔다.
