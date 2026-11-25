#!/bin/sh
# 실제 VPN 없이 확인할 수 있는 것만 확인한다. 먼저 `docker compose build` 를 해 둔다.
#   1) compose 설정이 맞고 포트가 127.0.0.1 에만 묶이는지
#   2) ovpn 이 없거나 비밀번호 파일이 없으면 명확한 오류로 멈추는지
#   3) OpenVPN 이 시작에 실패하면 멈추는지, ovpn 안의 명령 실행 지시를 거부하는지(F1),
#      DNS 설정을 못 쓰면 시작하지 않는지(F3)
#   4) 가짜 터널(상대 없는 p2p, 비밀값 없음): SOCKS 가 127.0.0.1 에만 열리고,
#      터널 밖 주소·컨테이너 자신(F2)으로는 못 나가고, 권한이 최소이고(F5),
#      재연결·OpenVPN 종료 때 SOCKS 가 닫히는지
# 쓰는 컨테이너 이름은 vpn-proxy-test-* 뿐이고 끝나면 지운다.
set -u
cd "$(dirname "$0")/.."
DOCKER="${DOCKER:-docker}"
IMAGE=vpn-proxy:local
# compose 와 같은 권한·설정으로 띄운다.
drun() {
  $DOCKER run --cap-drop ALL --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID --device /dev/net/tun \
    --sysctl net.ipv6.conf.all.disable_ipv6=1 --sysctl "net.ipv4.ping_group_range=0 2147483647" --tmpfs /run "$@"
}
TMP=$(mktemp -d "${TMPDIR:-/tmp}/vpn-proxy-test.XXXXXX") || exit 1
fail=0
ok() { echo "  통과: $*"; }
bad() { echo "  실패: $*"; fail=1; }
cleanup() {
  for c in $($DOCKER ps -aq --filter name=vpn-proxy-test-); do $DOCKER rm -f "$c" >/dev/null 2>&1; done
  case "$TMP" in */vpn-proxy-test.*) rm -rf "$TMP" ;; esac
}
trap cleanup EXIT

run_once() { # 이름, 폴더, 추가 옵션… → 출력은 $out, 종료 코드는 $rc
  n="$1"; d="$2"; shift 2
  out=$(drun --rm --name "vpn-proxy-test-$n" -e VPN_FAIL_SLEEP=0 "$@" -v "$d:/vpn:ro" $IMAGE 2>&1)
  rc=$?
}

echo "1) compose 설정"
if $DOCKER compose config | grep -q NET_RAW; then bad "compose 에 NET_RAW 가 있음"; else ok "compose 에 NET_RAW 없음"; fi
if grep -Eq '^FROM alpine:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$' Dockerfile; then ok "베이스 이미지가 정확한 버전과 다이제스트로 고정됨"; else bad "베이스 이미지가 고정되지 않음"; fi
if $DOCKER compose config -q; then ok "compose config"; else bad "compose config"; fi
binds=$($DOCKER compose config --format json | grep -o '"host_ip": *"[^"]*"' | sort -u)
if [ "$binds" = '"host_ip": "127.0.0.1"' ]; then ok "모든 포트가 127.0.0.1 에만 묶임"; else bad "127.0.0.1 이 아닌 바인드: $binds"; fi

echo "2) 설정 파일 오류"
mkdir -p "$TMP/empty" "$TMP/needauth" "$TMP/two" "$TMP/dead" "$TMP/fake"
run_once empty "$TMP/empty"
if [ $rc -ne 0 ] && echo "$out" | grep -q "ovpn 파일이 없습니다"; then ok "ovpn 없음 → 종료 코드 $rc, 안내 문구"; else bad "ovpn 없음: rc=$rc out=$out"; fi

printf 'client\ndev tun\nremote 192.0.2.1 1194 udp\nauth-user-pass\n' > "$TMP/needauth/client.ovpn"
run_once auth "$TMP/needauth"
if [ $rc -ne 0 ] && echo "$out" | grep -q "auth.txt"; then ok "auth-user-pass 인데 auth.txt 없음 → 종료 코드 $rc, 안내 문구"; else bad "auth 없음: rc=$rc out=$out"; fi

printf 'remote 192.0.2.1\n' > "$TMP/two/a.ovpn"
printf 'remote 192.0.2.1\n' > "$TMP/two/b.ovpn"
run_once two "$TMP/two"
if [ $rc -ne 0 ] && echo "$out" | grep -q "1개만"; then ok ".ovpn 2개 → 종료 코드 $rc, 안내 문구"; else bad "ovpn 2개: rc=$rc out=$out"; fi

out=$($DOCKER run --rm --name vpn-proxy-test-nocap --cap-drop ALL --tmpfs /run -e VPN_FAIL_SLEEP=0 -e VPN_CONFIG=a.ovpn -v "$TMP/two:/vpn:ro" $IMAGE 2>&1); rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -Eq "/dev/net/tun|방화벽"; then ok "권한·장치 없음 → 시작하지 않음(종료 코드 $rc)"; else bad "권한 없음: rc=$rc out=$out"; fi

echo "3) 연결되지 않는 VPN"
# 192.0.2.1 은 문서용 주소라 응답이 없다. 인증서 자리는 비워 둔 TLS 클라이언트라 계속 시도만 한다.
printf 'client\ndev tun\nremote 192.0.2.1 1194 udp\nca /nonexistent-on-purpose.crt\n' > "$TMP/dead/client.ovpn"
run_once dead "$TMP/dead"
if [ $rc -ne 0 ] && echo "$out" | grep -q "연결 전에 끝났습니다"; then ok "OpenVPN 이 시작에 실패 → 종료 코드 $rc, 안내 문구"; else bad "OpenVPN 실패: rc=$rc out=$out"; fi

# 상대가 없는 p2p 설정: tun0 만 올라온다. 비밀값이 없고 어디에도 연결되지 않는다.
printf 'dev tun\nremote 192.0.2.1 1194 udp\nifconfig 10.99.0.2 10.99.0.1\nroute 10.99.1.0 255.255.255.0\n' > "$TMP/fake/client.ovpn"

echo "3-1) ovpn 안의 명령 실행 지시는 받지 않는다 (F1)"
BASE='dev tun\nremote 192.0.2.1 1194 udp\nifconfig 10.99.0.2 10.99.0.1\n'
reject() { # 설명, 줄 번호, ovpn 에 더할 내용
  d="$TMP/rej"; mkdir -p "$d"
  printf "$BASE$3" > "$d/client.ovpn"
  run_once rej "$d"
  if [ $rc -ne 0 ] && echo "$out" | grep -q "$2번째 줄" && echo "$out" | grep -q "시작하지 않습니다" && ! echo "$out" | grep -q "OpenVPN 시작"; then
    ok "거부: $1 ($2번째 줄 표시)"
  else
    bad "거부되지 않음: $1 → rc=$rc out=$out"
  fi
}
reject "route-up"                  4 'route-up "/usr/sbin/iptables -P OUTPUT ACCEPT"\n'
reject "대문자 ROUTE-UP"           4 'ROUTE-UP "/usr/sbin/iptables -P OUTPUT ACCEPT"\n'
reject "앞 공백·탭"                4 '  \tipchange /bin/true\n'
reject "앞에 -- 를 붙인 지시"      4 '--tls-verify /bin/true\n'
reject "<connection> 블록 안"      5 '<connection>\nroute-pre-down /bin/true\nremote 192.0.2.1 1194 udp\n</connection>\n'
reject "블록으로 쓴 <route-up>"    4 '<route-up>\n/usr/sbin/iptables -P OUTPUT ACCEPT\n</route-up>\n'
reject "따옴표로 감싼 지시 이름"   4 '"route-up" /bin/true\n'
reject "역슬래시를 넣은 지시 이름" 4 'rou\\te-up /bin/true\n'
reject "plugin"                    4 'plugin /vpn/evil.so\n'
reject "script-security"           4 'script-security 3\n'
reject "up"                        4 'up /bin/true\n'
reject "auth-user-pass-verify"     4 'auth-user-pass-verify /bin/true via-env\n'
reject "client-connect"            4 'client-connect /bin/true\n'
reject "learn-address"             4 'learn-address /bin/true\n'
reject "config(다른 파일 불러오기)" 4 'config /vpn/other.conf\n'
reject "setenv PATH"               4 'setenv PATH /vpn\n'
printf "${BASE}<ca>\nMIIB\nroute-up /bin/true\n" > "$TMP/rej/client.ovpn"
run_once rej "$TMP/rej"
if [ $rc -ne 0 ] && echo "$out" | grep -q "닫히지 않았습니다" && ! echo "$out" | grep -q "OpenVPN 시작"; then ok "거부: 닫히지 않은 블록(뒤에 지시를 숨길 수 없음)"; else bad "닫히지 않은 블록이 통과: $out"; fi

# 정상 ovpn 은 검사를 지나가야 한다: 인증서 블록 안의 글자는 지시로 보지 않는다.
mkdir -p "$TMP/okcfg"
printf "${BASE}# 주석 up\n;route-up 주석\nsetenv opt block-outside-dns\nverb 3\n<ca>\ndown\nplugin\n</ca>\n" > "$TMP/okcfg/client.ovpn"
run_once okcfg "$TMP/okcfg"
if echo "$out" | grep -q "OpenVPN 시작" && ! echo "$out" | grep -q "받지 않는"; then ok "정상 ovpn(주석·인증서 블록·setenv opt)은 검사를 통과"; else bad "정상 ovpn 이 막힘: $out"; fi

echo "3-2) DNS 설정을 못 쓰면 시작하지 않는다 (F3)"
out=$(drun --rm --name vpn-proxy-test-ro --read-only -e VPN_FAIL_SLEEP=0 -v "$TMP/fake:/vpn:ro" $IMAGE 2>&1); rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "resolv.conf 를 쓸 수 없습니다" && ! echo "$out" | grep -q "SOCKS5 시작"; then ok "읽기 전용 파일 시스템 → SOCKS 를 켜지 않고 종료 코드 $rc"; else bad "읽기 전용인데 진행됨: rc=$rc out=$out"; fi

echo "4) 가짜 터널(상대 없음)"
drun -d --name vpn-proxy-test-fake -e VPN_DNS=10.99.0.53 -p 127.0.0.1::1080 -v "$TMP/fake:/vpn:ro" $IMAGE >/dev/null
i=0
while [ $i -lt 20 ]; do
  $DOCKER logs vpn-proxy-test-fake 2>&1 | grep -q "SOCKS5 시작" && break
  sleep 1; i=$((i + 1))
done
if $DOCKER logs vpn-proxy-test-fake 2>&1 | grep -q "SOCKS5 시작"; then
  ok "터널이 올라온 뒤 SOCKS5 시작"
  rules=$($DOCKER exec vpn-proxy-test-fake iptables -S 2>&1)
  if echo "$rules" | grep -q -- "-P OUTPUT DROP" && echo "$rules" | grep -q -- "-P INPUT DROP" && echo "$rules" | grep -q -- "-P FORWARD DROP"; then ok "방화벽 기본 정책 DROP"; else bad "기본 정책: $rules"; fi
  if echo "$rules" | grep -q -- "-A OUTPUT -d 192.0.2.1/32 ! -o tun+ -p udp -m udp --dport 1194 -j ACCEPT"; then ok "터널 밖 허용은 VPN 서버 주소·포트뿐"; else bad "서버 허용 규칙 없음: $rules"; fi
  if $DOCKER exec vpn-proxy-test-fake sh -c 'nc -w 3 1.1.1.1 443 </dev/null' >/dev/null 2>&1; then bad "터널 밖 TCP 가 나감"; else ok "컨테이너 안에서 터널 밖 TCP 차단(1.1.1.1:443)"; fi
  if $DOCKER exec vpn-proxy-test-fake sh -c 'dig +time=2 +tries=1 @1.1.1.1 example.com' >/dev/null 2>&1; then bad "터널 밖 DNS 가 나감"; else ok "컨테이너 안에서 터널 밖 DNS 차단(1.1.1.1:53)"; fi

  if [ "$($DOCKER exec vpn-proxy-test-fake cat /etc/resolv.conf)" = "nameserver 10.99.0.53" ]; then ok "DNS 가 지정한 터널 안 주소로 바뀜(VPN_DNS)"; else bad "resolv.conf 가 바뀌지 않음"; fi

  port=$($DOCKER port vpn-proxy-test-fake 1080/tcp)
  case "$port" in 127.0.0.1:*) ok "호스트 바인드 $port" ;; *) bad "호스트 바인드가 127.0.0.1 이 아님: $port" ;; esac
  hp=${port##*:}
  # SOCKS 로 인터넷 주소에 붙어 본다. 터널 밖이라 실패해야 한다(프록시 자체에는 붙는다).
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 --socks5-hostname "127.0.0.1:$hp" http://1.1.1.1/ 2>/dev/null); crc=$?
  if [ $crc -ne 0 ] && [ "$code" = "000" ]; then ok "SOCKS 로 터널 밖 주소 접속 실패(curl 종료 코드 $crc)"; else bad "SOCKS 로 터널 밖 주소가 열림: http=$code rc=$crc"; fi
  if $DOCKER exec vpn-proxy-test-fake sh -c 'pidof sockd' >/dev/null 2>&1; then ok "SOCKS 서버(sockd) 실행 중"; else bad "sockd 가 떠 있지 않음"; fi
  # 터널 안 주소로는 중계가 돼야 한다. 컨테이너의 터널 주소에 한 줄짜리 서버를 띄우고 SOCKS 로 읽는다.
  $DOCKER exec -d vpn-proxy-test-fake sh -c 'printf "HTTP/1.0 200 OK\r\nContent-Length: 9\r\n\r\nvia-socks" | nc -l -p 8099 -s 10.99.0.2'
  sleep 1
  body=$(curl -s --max-time 6 --socks5-hostname "127.0.0.1:$hp" http://10.99.0.2:8099/ 2>/dev/null); crc=$?
  if [ "$body" = "via-socks" ]; then ok "SOCKS 로 터널 안 주소 중계됨"; else bad "터널 안 주소 중계 실패: rc=$crc body=$body"; fi

  # F2: 컨테이너 자신(127.0.0.0/8)으로는 중계하지 않는다. Docker 내장 DNS(127.0.0.11)가 여기에 있다.
  curl -s -o /dev/null --max-time 6 --noproxy '' --socks5 "127.0.0.1:$hp" http://127.0.0.11:53/ 2>/dev/null; crc=$?
  if [ $crc -eq 97 ] && $DOCKER logs vpn-proxy-test-fake 2>&1 | grep -q "block(1): tcp/connect.*127.0.0.11.53"; then ok "SOCKS 로 127.0.0.11:53 접속 차단(F2)"; else bad "127.0.0.11 로 중계됨: rc=$crc"; fi
  curl -s -o /dev/null --max-time 6 --noproxy '' --socks5 "127.0.0.1:$hp" http://127.0.0.1:1080/ 2>/dev/null; crc=$?
  if [ $crc -eq 97 ]; then ok "SOCKS 로 127.0.0.1 접속 차단(F2)"; else bad "127.0.0.1 로 중계됨: rc=$crc"; fi

  # F5: 권한과 상태 폴더
  caps=$($DOCKER exec vpn-proxy-test-fake grep CapEff /proc/1/status | tr -d '[:space:]')
  if [ "$caps" = "CapEff:00000000000010c0" ]; then ok "권한은 NET_ADMIN·SETUID·SETGID 뿐(NET_RAW 없음)"; else bad "권한 값이 다름: $caps"; fi
  if $DOCKER exec vpn-proxy-test-fake ping -c 1 -W 2 10.99.0.2 >/dev/null 2>&1; then ok "NET_RAW 없이 ping 동작"; else bad "ping 이 안 됨"; fi
  if [ "$($DOCKER exec vpn-proxy-test-fake stat -c %a /run/vpn)" = "755" ]; then ok "/run/vpn 은 root 만 씀(755)"; else bad "/run/vpn 권한이 다름"; fi
  if $DOCKER exec -u nobody vpn-proxy-test-fake sh -c 'echo x >> /run/vpn/sockd.conf || touch /run/vpn/new' >/dev/null 2>&1; then bad "nobody 가 /run/vpn 을 고칠 수 있음"; else ok "nobody 는 sockd.conf 를 못 고치고 새 파일도 못 만듦"; fi

  # F3: 터널이 내려갈 때 DNS 를 Docker DNS 로 되돌리지 않고 비운다. SOCKS 도 바로 꺼진다.
  $DOCKER exec vpn-proxy-test-fake /usr/local/bin/vpn-down.sh
  if $DOCKER exec vpn-proxy-test-fake grep -q nameserver /etc/resolv.conf; then bad "터널이 내려간 뒤 DNS 가 남아 있음"; else ok "터널이 내려가면 DNS 를 비움(F3)"; fi
  sleep 1
  if $DOCKER exec vpn-proxy-test-fake pidof sockd >/dev/null 2>&1; then bad "터널 내려감 표시 뒤에도 SOCKS 가 남음"; else ok "터널 내려감 → 1초 안에 SOCKS 종료"; fi

  # 재연결(SIGUSR1): 터널이 내려간 동안 SOCKS 가 꺼지고, 다시 올라오면 켜져야 한다.
  $DOCKER exec vpn-proxy-test-fake pkill -USR1 openvpn
  sleep 10
  logs=$($DOCKER logs vpn-proxy-test-fake 2>&1)
  if echo "$logs" | grep -q "SOCKS5 중단" && [ "$(echo "$logs" | grep -c "SOCKS5 시작")" -ge 2 ] && $DOCKER exec vpn-proxy-test-fake pidof sockd >/dev/null 2>&1; then ok "재연결: SOCKS 중단 후 다시 시작"; else bad "재연결 동작 이상"; fi
  if [ "$($DOCKER exec vpn-proxy-test-fake cat /etc/resolv.conf)" = "nameserver 10.99.0.53" ]; then ok "재연결 뒤 DNS 는 다시 터널 안 주소"; else bad "재연결 뒤 DNS 가 다름"; fi

  # OpenVPN 을 죽이면(터널 끊김) SOCKS 도 닫혀야 한다.
  $DOCKER exec vpn-proxy-test-fake sh -c 'kill -9 $(pidof openvpn)' >/dev/null 2>&1
  sleep 5
  if $DOCKER exec vpn-proxy-test-fake sh -c 'pidof sockd' >/dev/null 2>&1; then bad "OpenVPN 이 죽었는데 SOCKS 가 남아 있음"; else ok "OpenVPN 종료 → SOCKS 도 종료(kill-switch)"; fi
else
  bad "SOCKS5 가 시작되지 않음: $($DOCKER logs vpn-proxy-test-fake 2>&1 | tail -15)"
fi

echo
if [ $fail -eq 0 ]; then echo "결과: 모두 통과"; else echo "결과: 실패 있음"; fi
exit $fail
