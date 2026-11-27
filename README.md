# vpn-proxy

내부 대역이 서로 겹치는 OpenVPN 여러 개를 Mac 에서 **동시에** 쓴다.
Mac 에는 VPN 을 켜지 않는다. VPN마다 컨테이너 하나(OpenVPN 클라이언트 + SOCKS5 프록시)를 띄우고,
어느 VPN 으로 갈지는 **로컬 포트**로 고른다.

고정된 VPN 은 없다. 처음에는 0개이고, 웹 화면에서 `.ovpn` 을 올리는 대로 하나씩 생긴다. 예를 들어 `a`, `b`, `c` 라는
이름으로 세 개를 올리면 이렇게 된다(아래 예시는 모두 이 이름을 쓴다):

| 로컬 주소 | VPN(이름) | 컨테이너 | ovpn 폴더 |
|---|---|---|---|
| `127.0.0.1:1081` | a | `vpn-proxy-a` | `ovpn/a/` |
| `127.0.0.1:1082` | b | `vpn-proxy-b` | `ovpn/b/` |
| `127.0.0.1:1083` | c | `vpn-proxy-c` | `ovpn/c/` |

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

컨테이너마다 네트워크가 따로라서 VPN 들의 대역이 같아도 부딪히지 않는다.
같은 `10.0.0.10` 이라도 1081 로 가면 A 의 서버, 1082 로 가면 B 의 서버다.

컨테이너 안의 안전장치:

- **방화벽**: 나가는 길은 터널(`tun0`)과 VPN 서버 주소뿐이다. 터널이 없으면 아무 데도 못 나간다.
- **kill-switch**: 터널이 내려가면 SOCKS5 를 끄고, 다시 올라오면 켠다. OpenVPN 이 죽으면 컨테이너가 끝나고 다시 뜬다.
- **DNS**: VPN 이 알려 준 DNS 만 쓴다. 없으면 이름 해석을 하지 않는다(터널 밖으로 이름이 새지 않게).
- **ovpn 검사**: 허용 목록에 있는 지시만 받는다. 그 밖의 지시(`route-up`, `plugin` 등)가 있으면 시작하지 않는다.
- **스크립트 실행 안 함**: OpenVPN 을 `--script-security 1` 로 띄운다. 검사와 별개로 한 겹 더 막는다.
- **포트**: Mac 의 `127.0.0.1` 에만 연다. 같은 네트워크의 다른 기기는 붙을 수 없다. SOCKS5 에 비밀번호는 없다.

## 준비물

- Docker Desktop 또는 OrbStack (`docker compose` 사용), Mac 에 이미 있는 `python3`
- VPN별 `.ovpn` 파일. 이 저장소에는 들어 있지 않고, 넣어도 git 에 올라가지 않는다(`.gitignore`).
  **믿을 수 있는 출처(회사에서 직접 받은 것)의 ovpn 만 넣는다.** ovpn 은 VPN 서버 주소와 경로를 정하는
  파일이라, 남이 만든 ovpn 을 넣으면 그쪽 서버로 연결된다. 아래 "ovpn 검사"는 명령 실행을 막을 뿐이다.

## 빠른 시작 (3단계)

**1. 이미지를 만들고 화면을 띄운다.**

```sh
docker build -t vpn-proxy:local .    # 처음 한 번
python3 gui/server.py                # http://127.0.0.1:8030   끝내기: Ctrl+C
```

**2. 브라우저에서 `.ovpn` 을 올린다.** `http://127.0.0.1:8030` 아래쪽 'VPN 추가'에서 파일을 고르고 이름을 적은 뒤
'올리기'. SOCKS5 포트가 1081 부터 자동으로 배정된다. 카드의 '켜기'를 누르면 연결한다. VPN 마다 되풀이한다.

- 아이디·비밀번호를 묻는 VPN(`auth-user-pass`)은 같은 칸에서 함께 적는다.
- 인증서·키가 ovpn 과 따로 떨어진 파일이면 화면으로는 올릴 수 없다. "VPN 추가(직접)"을 본다.

**3. 확인한다.** 카드가 '연결됨'이면 된 것이다. 터미널에서는:

```sh
curl --socks5-hostname 127.0.0.1:1081 http://10.0.0.10/    # 1081 에 붙은 VPN 안의 서버 (주소는 예시)
```

### 터미널에서 docker compose 쓰기

이 저장소의 `docker-compose.yml` 에는 VPN 이 적혀 있지 않다(모든 서비스가 따르는 틀 `vpn-template` 만 있다).
등록한 VPN 은 `compose.generated.yml` 에 서비스로 생긴다. 그래서 두 파일을 함께 넘긴다:

```sh
docker compose -f docker-compose.yml -f compose.generated.yml ps        # 떠 있는 VPN 이 (healthy) 면 연결된 것
docker compose -f docker-compose.yml -f compose.generated.yml logs -f a # "SOCKS5 시작" 이 보이면 준비 끝
```

매번 적기 번거로우면 `cp .env.example .env` 한다. 그 안의 `COMPOSE_FILE=docker-compose.yml:compose.generated.yml`
덕분에 `docker compose ps` 처럼 `-f` 없이 쓸 수 있다. 아래 예시는 이렇게 해 두었다고 보고 `-f` 를 뺐다.

멈추기: `docker compose down`. 하나만 다시: `docker compose restart b`.

## 웹 화면

VPN 추가(.ovpn 올리기), 켜고 끄기, 상태 확인, 삭제를 브라우저에서 한다. Mac 에서 도는 작은 서버가 이 폴더의 `docker compose` 를 대신 부른다.
필요한 것은 Mac 에 이미 있는 `python3` 뿐이다(설치할 것 없음).

```sh
docker build -t vpn-proxy:local .    # 처음 한 번(화면의 '켜기'는 이미지를 빌드하지 않는다)
python3 gui/server.py                # http://127.0.0.1:8030   끝내기: Ctrl+C
VPN_GUI_PORT=8031 python3 gui/server.py
```

화면에 보이는 것: 등록된 VPN 목록, 켜기·끄기 버튼, 상태(연결됨 / 연결 중 / 실패와 사유 / 꺼짐),
SOCKS5 주소·포트, ovpn 폴더와 `.ovpn` 파일 수, 최근 로그 40줄.

VPN 카드의 제목은 그 폴더에 넣은 `.ovpn` 파일 이름(확장자 뺌)이고, 부제는 그 ovpn 이 접속하는 서버
(`remote` 의 호스트:포트/프로토콜)다. 등록한 이름과 로컬 포트는 그 아래 작게 나온다.
화면 서버는 ovpn 에서 `remote`·`port`·`proto` 줄만 읽는다(인증서·키 블록은 읽어 쓰지 않는다).
`.ovpn` 이 없거나 2개 이상이면 제목은 폴더 이름이다.

- 켜기 = `docker compose -f docker-compose.yml -f compose.generated.yml up -d --no-build <이름>`, 끄기 = 같은 방식의 `stop <이름>`.
- 실패한 VPN 은 컨테이너가 10초마다 다시 시도한다. 멈추려면 끄기를 누른다.
- VPN 을 늘리려면 화면 아래 'VPN 추가'에서 `.ovpn` 을 올린다(아래 "웹에서 VPN 추가").
- ovpn 검사는 올릴 때 한 번, 컨테이너가 시작할 때 또 한 번 돈다. 거부되면 사유(줄 번호)가 화면에 뜬다.

### 웹에서 VPN 추가

1. 화면 아래 'VPN 추가'에서 `.ovpn` 파일을 고른다(256KB 까지).
2. 이름을 적는다 — 소문자·숫자·`-` 로 1~24자(예: `office`). 파일 이름에서 자동으로 채워진다.
3. ovpn 에 `auth-user-pass` 줄이 있으면 아이디·비밀번호를 적는다. 없으면 비워 둔다.
4. '올리기'를 누른다. 등록되면 목록에 새 카드가 생기고 SOCKS5 포트가 배정된다. '켜기'를 누르면 연결한다.

일어나는 일:

- 저장: `ovpn/<이름>/<이름>.ovpn`(권한 0600, 폴더 0700). 아이디·비밀번호는 `ovpn/<이름>/auth.txt`(0600).
- 검사: 저장한 파일을 `rootfs/check-ovpn.sh`(컨테이너가 쓰는 그 검사)로 검사한다. 통과하지 못하면 폴더째 지우고
  사유를 줄 번호와 함께 보여 준다("ovpn 검사" 절 참고).
- 포트: `127.0.0.1` 의 1081~1180 중 비어 있는 가장 작은 번호. `127.0.0.1` 로만 연다.
- 등록: 이름·포트·폴더를 `vpns.json` 에 적고, 거기서 `compose.generated.yml` 을 만든다. 두 파일 모두 git 에
  올라가지 않고 손으로 고치지 않는다. `docker-compose.yml` 은 바뀌지 않는다.
- 교체: 같은 이름으로 다시 올리면서 '같은 이름이 이미 있으면 그 ovpn 을 교체'를 고르면 ovpn(과 아이디·비밀번호)만
  바뀌고 포트는 그대로다. 새 것이 검사를 통과하지 못하면 기존 것을 그대로 둔다. 켜져 있으면 껐다 켜야 적용된다.
- 삭제: 카드에서 '삭제' → '삭제 확인'. 컨테이너를 멈추고 지운 뒤 `ovpn/<이름>/` 폴더를 지운다.
  되돌릴 수 없다. Docker 가 꺼져 있으면 지우지 않는다. 직접 넣은 폴더("VPN 추가(직접)")는 등록만 지우고 폴더는 남긴다.

받는 ovpn:

- 인증서·키가 ovpn 안에 `<ca>`·`<cert>`·`<key>`·`<tls-crypt>` 블록으로 들어 있는 것. 올릴 수 있는 것은 파일 하나뿐이라
  `ca ca.crt` 처럼 다른 파일을 가리키는 줄이 있으면 받지 않는다(그런 ovpn 은 아래 "VPN 추가(직접)" 방식으로 넣는다).
- 아이디·비밀번호는 파일 이름 없이 `auth-user-pass` 만 적힌 것(또는 `<auth-user-pass>` 블록).
- 이미 있는 이름·폴더는 새로 올릴 수 없다(교체를 고른 경우만 바꾼다).

비밀번호와 키: 한 번 저장하면 화면·응답·로그에 다시 내보내지 않는다(화면 서버에는 그 파일을 읽어 돌려주는 코드가 없다).
바꾸려면 지우고 다시 올린다.

포트나 DNS 를 바꾸려면: `python3 gui/vpnctl.py set <이름> --port 1090 --dns 10.0.0.2` (껐다 켜면 적용).

지키는 것:

- `127.0.0.1` 에만 연다. 주소는 바꿀 수 없고 포트만 바꾼다. 로그인은 없다 — 이 Mac 의 사용자만 쓴다고 본다.
- 상태를 바꾸는 요청은 POST 뿐이고, 이 화면 자신이 보낸 것(Origin·Host 가 `127.0.0.1`/`localhost` 의 그 포트)만 받는다.
  다른 웹사이트가 브라우저를 시켜 VPN 을 켜고 끌 수 없다.
- 켜고 끄고 지울 수 있는 대상은 등록부에 있는 VPN 뿐이다. 요청 값은 그 목록과 비교만 한다.
- 올리기·삭제도 같은 검사(POST, Origin·Host)를 거친다. 다른 웹사이트가 ovpn 을 올리거나 지울 수 없다.
- ovpn 에서는 접속 서버(`remote`·`port`·`proto`)만 읽고, `auth.txt` 는 읽지 않는다. 로그는 알려진 모양(`password=…`, `Authorization: …`, 주소 안의 비밀번호, 인증서·키 본문 등)만 가린다. 모든 비밀을 찾아내지는 못하므로 화면을 공유할 때는 로그를 접어 둔다.

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

VPN 이 DNS 를 알려 주지 않으면 로그에 경고가 뜨고 IP 로만 접속된다. 이때는
`python3 gui/vpnctl.py set a --dns 10.0.0.2` 처럼 내부 DNS 주소를 등록하고 껐다 켠다.

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

## VPN 추가(직접)

보통은 웹 화면에서 올린다("웹에서 VPN 추가"). 인증서·키가 따로 떨어진 파일인 ovpn 처럼 화면으로 올릴 수 없는 것은
폴더를 직접 만들고 명령줄로 등록한다.

1. `ovpn/<이름>/` 폴더를 만들고 `.ovpn` 1개와 그것이 가리키는 파일(`ca.crt` 등)을 넣는다. 이름은 소문자·숫자·`-`.
   아이디·비밀번호가 필요하면 같은 폴더에 `auth.txt`(첫 줄 아이디, 둘째 줄 비밀번호)를 둔다.
2. 등록한다. 포트를 적지 않으면 1081 부터 빈 번호가 배정된다.

   ```sh
   python3 gui/vpnctl.py import office --port 1084
   python3 gui/vpnctl.py list
   ```

   등록할 때 ovpn 검사를 하고, 폴더를 0700, 안의 파일을 0600 으로 좁힌다.
3. 웹 화면에서 켜거나 `docker compose -f docker-compose.yml -f compose.generated.yml up -d office`.

이렇게 넣은 것은 화면에 '직접 넣은 폴더'로 취급된다 — 화면에서 지우면 등록만 지우고 폴더는 남기며, 화면에서 교체하지 않는다.

그 밖의 명령: `set <이름> --port N --dns 주소,…`(바꾸기), `forget <이름>`(등록만 지우기).

### 예전 방식(고정 vpn-a/b/c)에서 옮기기

예전에는 `docker-compose.yml` 에 `vpn-a`, `vpn-b`, `vpn-c` 가 적혀 있었다. 그때 쓰던 `ovpn/vpn-a/` 같은 폴더는
그대로 두고 등록만 하면 된다. 포트를 예전과 같게 주면 브라우저·SSH 설정을 고칠 필요가 없다.

```sh
python3 gui/vpnctl.py import vpn-a --port 1081
python3 gui/vpnctl.py import vpn-b --port 1082
python3 gui/vpnctl.py import vpn-c --port 1083
docker rm -f vpn-proxy-a vpn-proxy-b vpn-proxy-c      # 예전 컨테이너(같은 포트를 쥐고 있다). 이때 잠깐 끊긴다
docker compose -f docker-compose.yml -f compose.generated.yml up -d
```

새 컨테이너 이름은 `vpn-proxy-<이름>` 이다(예: `vpn-proxy-vpn-a`). `.env` 에 `VPN_A_DNS` 를 적어 두었다면
`import` 에 `--dns` 로 옮기고, `.env` 의 `VPN_A_*`·`VPN_B_*`·`VPN_C_*` 줄은 지운다(이제 쓰지 않는다).

## 설정 값

VPN 별 값은 등록부(`vpns.json`)에 있고 웹 화면과 `gui/vpnctl.py` 로 바꾼다.

| 값 | 어디서 | 뜻 |
|---|---|---|
| 이름 | 올릴 때 / `import <이름>` | 서비스·폴더·로그의 이름. 컨테이너는 `vpn-proxy-<이름>` |
| 포트 | 자동(1081~) / `--port` | Mac 의 `127.0.0.1` 포트 |
| DNS | `--dns` | VPN 이 DNS 를 안 알려 줄 때 쓸 내부 DNS. 쉼표로 4개까지 |
| 아이디·비밀번호 | 올릴 때 / 폴더의 `auth.txt` | `auth-user-pass` 가 있는 ovpn 에만 |

`.env`(선택)에 둘 수 있는 것:

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `COMPOSE_FILE` | (없음) | `docker-compose.yml:compose.generated.yml` 로 두면 터미널에서 `-f` 없이 쓴다 |
| `VPN_CONNECT_TIMEOUT` | `60` | 연결을 기다리는 초 |

비밀번호는 `.env` 에 적지 않는다.

## 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| 로그에 `ovpn 파일이 없습니다` | `ovpn/<이름>/` 에 `.ovpn` 이 없다. 넣으면 10초쯤 뒤 저절로 다시 시도한다 |
| `.ovpn 파일이 2 개입니다` | 폴더에 1개만 남긴다 |
| `아이디·비밀번호가 필요합니다` | 같은 폴더에 `auth.txt` 를 만든다 |
| `ovpn 에 받지 않는 내용이 있습니다` | 로그에 찍힌 줄 번호의 지시·글자를 ovpn 에서 지운다(한글 주석 포함). 아래 "ovpn 검사" 참고 |
| `/etc/resolv.conf 를 쓸 수 없습니다` | 컨테이너를 읽기 전용(`read_only`)으로 띄우지 않는다 |
| `OpenVPN 이 연결 전에 끝났습니다` | 바로 위의 OpenVPN 로그를 본다. 인증서 경로, 만료, 비밀번호 오류가 흔하다 |
| `60초 안에 VPN 이 연결되지 않았습니다` | 서버 주소·포트, 회사 방화벽을 확인한다. 느린 VPN 은 `VPN_CONNECT_TIMEOUT` 을 늘린다 |
| `VPN 서버 주소를 하나도 찾지 못했습니다` | ovpn 의 `remote` 이름이 Mac 에서 풀리는지 본다(`dig <이름>`) |
| `/dev/net/tun 이 없습니다` | Docker Desktop·OrbStack 을 최신으로 올린다. compose 의 `devices`, `cap_add` 를 지우지 않았는지 본다 |
| curl 이 바로 끊긴다 | 터널이 아직 안 올라왔다. `docker compose ps` 가 healthy 인지, 로그에 `SOCKS5 시작` 이 있는지 본다 |
| IP 는 되는데 이름이 안 된다 | `socks5h` 로 썼는지, 로그에 DNS 경고가 있는지 본다. 있으면 `gui/vpnctl.py set <이름> --dns …` 로 DNS 를 적는다 |
| 인터넷 사이트가 안 열린다 | VPN 이 내부 대역만 받는 설정이면 정상이다. 터널 밖으로는 내보내지 않는다 |
| 포트가 이미 쓰이고 있다 | `python3 gui/vpnctl.py set <이름> --port <번호>` 로 바꾼다 |
| 연결은 되는데 특정 대역만 안 된다 | `docker exec vpn-proxy-a ip route` 로 VPN 이 그 대역 경로를 줬는지 본다. VPN 대역이 Docker 내부 대역(보통 `172.x`)과 겹치면 Docker 설정에서 기본 주소 대역을 바꾼다 |
| ovpn 의 `user nobody` 등으로 재연결이 실패한다 | 권한을 내린 뒤에는 경로를 다시 못 건다. `docker compose restart a` 로 다시 띄운다 |

상태를 한눈에:

```sh
docker compose ps
docker compose logs --tail 30 a
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
{"name":"a","state":"up","since":"2026-11-25T06:55:12Z","gen":1,"dns":["10.0.0.2"],"socks_port":1080,"message":"터널 연결됨"}
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
docker build -t vpn-proxy:local . && sh tests/smoke.sh
```

`vpn-proxy-test-*` 이름의 컨테이너와 네트워크만 만들고 끝나면 지운다. 실제 VPN 서버 접속은 이 스크립트로 확인되지 않는다.

## 파일

```
docker-compose.yml     모든 VPN 서비스가 따르는 틀(vpn-template). VPN 은 적혀 있지 않다
gui/server.py          웹 화면 서버(목록·상태·켜기·끄기·올리기·교체·삭제)
gui/vpnstore.py        VPN 등록부(저장·검사·compose 생성)
gui/vpnctl.py          등록부 명령줄(기존 폴더 가져오기, 포트·DNS 바꾸기)
vpns.json              등록한 VPN 의 이름·포트·폴더(git 제외, 자동 생성)
compose.generated.yml  vpns.json 에서 만든 compose 서비스(git 제외, 자동 생성)
Dockerfile             alpine + openvpn + dante(SOCKS5) + iptables
rootfs/entrypoint.sh   설정 확인 → ovpn 검사 → 방화벽 → OpenVPN → 터널 확인 → SOCKS5, 감시
rootfs/check-ovpn.sh   ovpn 검사(허용 목록)
rootfs/vpn-lib.sh      상태 기록, DNS 적용, OpenVPN 로그 따라가기
rootfs/healthcheck.sh  터널 + SOCKS 포트 확인
.env.example           COMPOSE_FILE, 연결 대기 시간
ovpn/<이름>/           VPN 별 ovpn 폴더(내용은 git 제외)
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

## 라이선스

MIT — `LICENSE` 파일 참고.
