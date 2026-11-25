#!/bin/sh
# 실제 VPN 없이 확인할 수 있는 것만 확인한다. 먼저 `docker compose build` 를 해 둔다.
#   1) compose 설정이 맞고 포트가 127.0.0.1 에만 묶이는지, 베이스 이미지가 고정됐는지
#   2) ovpn 이 없거나 비밀번호 파일이 없으면 명확한 오류로 멈추는지
#   3) ovpn 검사: 허용 목록 밖의 지시·글자·블록을 거부하는지(보안 검토의 우회 시도 포함),
#      정상 ovpn 은 통과하는지
#   4) 검사와 별개로 OpenVPN 이 스크립트를 실행하지 못하는지(script-security 1)
#   5) 서버가 내려 준 DNS 만 골라 쓰는지(로그 따라가기 함수 단독 시험)
#   6) 시험용 터널: 같은 이미지로 띄운 상대(peer) 컨테이너와 암호 없는 1:1 터널을 맺는다.
#      비밀값이 없고 이 Mac 안에서만 돈다. SOCKS 가 127.0.0.1 에만 열리고, 터널 안으로는 중계되고,
#      터널 밖·컨테이너 자신으로는 못 나가고, 권한이 최소이고, 터널이 끊기면 SOCKS 가 닫히는지
#   7) DNS 설정을 못 쓰면 시작하지 않는지
# 쓰는 컨테이너·네트워크 이름은 vpn-proxy-test-* 뿐이고 끝나면 지운다.
set -u
cd "$(dirname "$0")/.."
DOCKER="${DOCKER:-docker}"
IMAGE=vpn-proxy:local
NET=vpn-proxy-test-net
# compose 와 같은 권한·설정으로 띄운다.
drun() {
  $DOCKER run --cap-drop ALL --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges:true \
    --device /dev/net/tun --sysctl net.ipv6.conf.all.disable_ipv6=1 --sysctl "net.ipv4.ping_group_range=0 2147483647" --tmpfs /run "$@"
}
TMP=$(mktemp -d "${TMPDIR:-/tmp}/vpn-proxy-test.XXXXXX") || exit 1
fail=0
ok() { echo "  통과: $*"; }
bad() { echo "  실패: $*"; fail=1; }
remove_test_objects() {
  for c in $($DOCKER ps -aq --filter name=vpn-proxy-test-); do $DOCKER rm -f "$c" >/dev/null 2>&1; done
  $DOCKER network rm "$NET" >/dev/null 2>&1
}
cleanup() {
  remove_test_objects
  case "$TMP" in */vpn-proxy-test.*) rm -rf "$TMP" ;; esac
}
trap cleanup EXIT
remove_test_objects

run_once() { # 이름, 폴더, 추가 옵션… → 출력은 $out, 종료 코드는 $rc
  n="$1"; d="$2"; shift 2
  out=$(drun --rm --name "vpn-proxy-test-$n" -e VPN_FAIL_SLEEP=0 "$@" -v "$d:/vpn:ro" $IMAGE 2>&1)
  rc=$?
}
in_c() { $DOCKER exec vpn-proxy-test-client "$@"; }
wait_state() { # 상태 단어, 최대 초
  i=0
  while [ $i -lt "$2" ]; do
    [ "$(in_c cat /run/vpn/state 2>/dev/null)" = "$1" ] && return 0
    sleep 1; i=$((i + 1))
  done
  return 1
}

echo "1) compose 설정"
if $DOCKER compose config -q; then ok "compose config"; else bad "compose config"; fi
binds=$($DOCKER compose config --format json | grep -o '"host_ip": *"[^"]*"' | sort -u)
if [ "$binds" = '"host_ip": "127.0.0.1"' ]; then ok "모든 포트가 127.0.0.1 에만 묶임"; else bad "127.0.0.1 이 아닌 바인드: $binds"; fi
if $DOCKER compose config | grep -q NET_RAW; then bad "compose 에 NET_RAW 가 있음"; else ok "compose 에 NET_RAW 없음"; fi
if grep -Eq '^FROM alpine:[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}$' Dockerfile; then ok "베이스 이미지가 정확한 버전과 다이제스트로 고정됨"; else bad "베이스 이미지가 고정되지 않음"; fi

echo "2) 설정 파일 오류"
mkdir -p "$TMP/empty" "$TMP/needauth" "$TMP/two" "$TMP/dead" "$TMP/rej" "$TMP/okcfg" "$TMP/client" "$TMP/ro"
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

# 192.0.2.1 은 문서용 주소라 응답이 없다. 인증서 파일이 없어 OpenVPN 이 시작에 실패한다.
printf 'client\ndev tun\nremote 192.0.2.1 1194 udp\nca nonexistent-on-purpose.crt\n' > "$TMP/dead/client.ovpn"
run_once dead "$TMP/dead"
if [ $rc -ne 0 ] && echo "$out" | grep -q "연결 전에 끝났습니다"; then ok "OpenVPN 이 시작에 실패 → 종료 코드 $rc, 안내 문구"; else bad "OpenVPN 실패: rc=$rc out=$out"; fi

run_once badname "$TMP/dead" -e 'VPN_NAME=a"b'
if [ $rc -ne 0 ] && echo "$out" | grep -q "VPN_NAME"; then ok "VPN_NAME 에 따옴표 → 시작하지 않음"; else bad "VPN_NAME 검사 없음: $out"; fi
run_once baddns "$TMP/dead" -e VPN_DNS=127.0.0.11
if [ $rc -ne 0 ] && echo "$out" | grep -q "VPN_DNS"; then ok "VPN_DNS=127.0.0.11(Docker DNS) → 시작하지 않음"; else bad "VPN_DNS 검사 없음: $out"; fi

echo "3) ovpn 검사: 허용 목록 (F1·R1)"
BASE='dev tun\nremote 192.0.2.1 1194 udp\nifconfig 10.99.0.2 10.99.0.1\nroute 10.99.1.0 255.255.255.0\n'
CMD='"/usr/sbin/iptables -P OUTPUT ACCEPT"'
reject() { # 설명, ovpn 내용(printf 형식)
  printf "$2" > "$TMP/rej/client.ovpn"
  run_once rej "$TMP/rej"
  if [ $rc -ne 0 ] && echo "$out" | grep -q "받지 않는 내용이 있습니다" && echo "$out" | grep -q "시작하지 않습니다" && ! echo "$out" | grep -q "OpenVPN 시작"; then
    ok "거부: $1"
  else
    bad "거부되지 않음: $1 → rc=$rc out=$(echo "$out" | head -5)"
  fi
}
# 3-a) 명령을 실행하게 하는 지시와 그 변형
reject "route-up"                          "$BASE"'route-up '"$CMD"'\n'
reject "대문자 ROUTE-UP"                   "$BASE"'ROUTE-UP '"$CMD"'\n'
reject "앞 공백·탭 + ipchange"             "$BASE"'  \tipchange /bin/true\n'
reject "앞에 -- 를 붙인 지시"              "$BASE"'--tls-verify /bin/true\n'
reject "<connection> 블록 안의 지시"       "$BASE"'<connection>\nroute-pre-down /bin/true\nremote 192.0.2.1 1194 udp\n</connection>\n'
reject "블록으로 쓴 <route-up>"            "$BASE"'<route-up>\n/usr/sbin/iptables -P OUTPUT ACCEPT\n</route-up>\n'
reject "따옴표로 감싼 지시 이름"           "$BASE"'"route-up" /bin/true\n'
reject "역슬래시를 넣은 지시 이름"         "$BASE"'rou\\\\te-up /bin/true\n'
reject "setenv opt 로 감싼 route-up"       "$BASE"'setenv opt route-up '"$CMD"'\n'
reject "setenv opt 두 번 감싼 up"          "$BASE"'setenv opt setenv opt up /bin/true\n'
reject "setenv PATH"                       "$BASE"'setenv PATH /vpn\n'
reject "setenv-safe"                       "$BASE"'setenv-safe PATH /vpn\n'
for d in up down tls-verify auth-user-pass-verify client-connect learn-address tls-crypt-v2-verify; do
  reject "$d" "$BASE$d"' /bin/true\n'
done
# 3-b) 라이브러리·다른 설정·원격 제어·파일 쓰기
reject "plugin"                            "$BASE"'plugin /vpn/evil.so\n'
reject "providers (보안 검토 R1)"          "$BASE"'providers /vpn/evil.so\n'
reject "engine (보안 검토 R1)"             "$BASE"'engine dynamic\n'
reject "script-security"                   "$BASE"'script-security 3\n'
reject "config(다른 파일 불러오기)"        "$BASE"'config /vpn/other.conf\n'
reject "management"                        "$BASE"'management 0.0.0.0 7505\n'
reject "log(파일 쓰기)"                    "$BASE"'log /etc/passwd\n'
reject "목록에 없는 지시(echo)"            "$BASE"'echo hello\n'
reject "dhcp-option DNS 127.0.0.11"        "$BASE"'dhcp-option DNS 127.0.0.11\n'
# 3-c) 글자: 검사와 OpenVPN 이 줄을 다르게 읽게 만드는 바이트 (보안 검토 R1)
reject "줄 앞 세로 탭(0x0B) + route-up"    "$BASE"'\013route-up '"$CMD"'\n'
reject "줄 앞 폼 피드(0x0C) + route-up"    "$BASE"'\014route-up '"$CMD"'\n'
reject "줄 앞 CR(0x0D) + route-up"         "$BASE"'\rroute-up '"$CMD"'\n'
reject "파일 맨 앞 BOM + route-up"         '\357\273\277route-up '"$CMD"'\n'"$BASE"
reject "route-up + 세로 탭 + 경로"         "$BASE"'route-up\013/vpn/x.sh\n'
reject "NUL 문자"                          "$BASE"'\000route-up '"$CMD"'\n'
reject "한글 주석(ASCII 아님)"             "$BASE"'# 주석\n'
long=$(printf '%0254d' 0 | tr 0 a)
reject "255자를 넘는 주석 줄 뒤에 숨긴 지시" "$BASE"'#'"$long"'route-up '"$CMD"'\n'
reject "CRLF 줄 끝의 route-up"             "$BASE"'route-up /vpn/x.sh\r\n'
# 3-d) 블록 닫는 표시 (보안 검토 R1)
reject "닫는 표시 뒤에 글자(</ca>x)"       "$BASE"'<ca>\nx\n</ca>x\nroute-up '"$CMD"'\n<ca>\nx\n</ca>\n'
reject "닫는 표시 뒤에 공백+글자"          "$BASE"'<ca>\nx\n</ca> x\nroute-up '"$CMD"'\n<ca>\nx\n</ca>\n'
reject "들여쓴 닫는 표시 뒤에 글자"        "$BASE"'<secret>\nx\n\t </secret> x\nroute-up '"$CMD"'\n<secret>\nx\n</secret>\n'
reject "닫는 표시 앞에 세로 탭"            "$BASE"'<ca>\nx\n\013</ca>\nroute-up '"$CMD"'\n'
reject "여는 표시 뒤에 글자(<ca> x)"       "$BASE"'<ca> x\nroute-up '"$CMD"'\n</ca>\n'
reject "대문자 블록 <CA>"                  "$BASE"'<CA>\nroute-up '"$CMD"'\n</CA>\n'
reject "닫히지 않은 블록"                  "$BASE"'<ca>\nMIIB\nroute-up /bin/true\n'
reject "닫히지 않은 <connection>"          "$BASE"'<connection>\nremote 192.0.2.1\n'

# 3-f) 파일 경로 인자는 ovpn 폴더 안으로 제한 (보안 검토 L3)
reject "ca /etc/shadow(절대 경로)"          "$BASE"'ca /etc/shadow\n'
reject "auth-user-pass /proc/1/environ"     "$BASE"'auth-user-pass /proc/1/environ\n'
reject "tls-crypt /run/vpn/client.ovpn"     "$BASE"'tls-crypt /run/vpn/client.ovpn\n'
reject "cert ../../etc/passwd(.. 로 벗어남)" "$BASE"'cert ../../etc/passwd\n'
reject "key sub/../../etc/passwd"           "$BASE"'key sub/../../etc/passwd\n'
reject "따옴표로 감싼 절대 경로"            "$BASE"'ca "/etc/shadow"\n'
reject "역슬래시를 넣은 절대 경로"          "$BASE"'ca \\\\/etc/shadow\n'
reject "탭으로 띄운 절대 경로"              "$BASE"'tls-auth\t/etc/shadow 1\n'
reject "setenv opt 로 감싼 절대 경로"       "$BASE"'setenv opt ca /etc/shadow\n'
reject "<connection> 안의 절대 경로"        "$BASE"'<connection>\nremote 192.0.2.1 1194 udp\n</connection>\npkcs12 /etc/shadow\n'
reject "crl-verify /etc dir"                "$BASE"'crl-verify /etc dir\n'
# 폴더 안의 상대 경로와 인라인 블록은 검사를 지나간다(검사 스크립트만 따로 실행).
printf "$BASE"'ca ca.crt\ncert certs/client.crt\nkey certs/client.key\ntls-auth ta.key 1\ntls-crypt tc.key\nauth-user-pass\nauth-user-pass login.txt\ndh none\ncrl-verify crl.pem\n<ca>\n/etc/shadow\n</ca>\n<tls-crypt>\nx\n</tls-crypt>\n' > "$TMP/okcfg/paths.ovpn"
if o=$($DOCKER run --rm --name vpn-proxy-test-paths --entrypoint check-ovpn.sh -v "$TMP/okcfg:/vpn:ro" $IMAGE /vpn/paths.ovpn 2>&1); then ok "폴더 안 상대 경로·인라인 블록·인자 없는 auth-user-pass·dh none 은 통과"; else bad "정상 경로가 막힘: $o"; fi
rm -f "$TMP/okcfg/paths.ovpn"

# 3-e) 정상 ovpn 은 검사를 지나가야 한다: 주석, CRLF, 인증서 블록 안의 글자, 흔한 클라이언트 지시.
printf 'client\r\ndev tun\r\nproto udp\r\nremote 192.0.2.1 1194\r\nresolv-retry infinite\r\nnobind\r\npersist-key\r\npersist-tun\r\nremote-cert-tls server\r\ncipher AES-256-GCM\r\ndata-ciphers AES-256-GCM:AES-128-GCM\r\nauth SHA256\r\nverb 3\r\nkey-direction 1\r\nsetenv opt block-outside-dns\r\nsetenv CLIENT_CERT 0\r\ndhcp-option DNS 10.8.0.1\r\nredirect-gateway def1\r\n# comment up\r\n;route-up comment\r\n<ca>\r\ndown\r\nplugin\r\n  </ca>  \r\n<connection>\r\nremote 192.0.2.1 443 tcp\r\n</connection>\r\n' > "$TMP/okcfg/client.ovpn"
run_once okcfg "$TMP/okcfg"
if echo "$out" | grep -q "OpenVPN 시작" && ! echo "$out" | grep -q "받지 않는"; then ok "정상 ovpn(CRLF·주석·인증서 블록·흔한 지시)은 검사를 통과"; else bad "정상 ovpn 이 막힘: $out"; fi

# 3-g) user·group 빼기는 인증서 블록 안쪽을 건드리지 않는다(블록 안 5번째 줄은 그대로, 7·8번째 줄만 뺀다).
printf 'client\ndev tun\nremote 192.0.2.1 1194 udp\n<ca>\nuser nobody\n</ca>\ngroup nogroup\n\t setenv opt  setenv opt\tuser x\nusername-as-common-name-not-a-directive\n' > "$TMP/dead/client.ovpn"
run_once ug "$TMP/dead"
if echo "$out" | grep -q '받지 않는 지시 "username-as-common-name-not-a-directive"'; then
  printf 'client\ndev tun\nremote 192.0.2.1 1194 udp\n<ca>\nuser nobody\n</ca>\ngroup nogroup\n\t setenv opt  setenv opt\tuser x\n' > "$TMP/dead/client.ovpn"
  run_once ug "$TMP/dead"
  if [ "$(echo "$out" | grep -c 'user/group 은')" = "2" ] && echo "$out" | grep -q '줄 7: user/group' && echo "$out" | grep -q '줄 8: user/group' && ! echo "$out" | grep -q '줄 5: user/group'; then ok "user·group 빼기: 블록 안쪽은 그대로, 겹친 setenv opt 도 뺌"; else bad "user/group 빼기: $out"; fi
else
  bad "user 로 시작하는 다른 낱말이 검사에서 걸리지 않음: $out"
fi

echo "4) 검사와 별개로 스크립트는 실행되지 않는다 (script-security 1)"
if grep -q -- '--script-security 1 ' rootfs/entrypoint.sh && ! grep -Eq -- '--(up|down|route-up)[ "]' rootfs/entrypoint.sh; then ok "entrypoint 는 --script-security 1 이고 훅 스크립트를 넘기지 않음"; else bad "entrypoint 의 OpenVPN 옵션이 다름"; fi
# 검사를 거치지 않고 OpenVPN 에 route-up 이 든 설정을 직접 준다. 1 이면 실행되지 않고, 2 면 실행된다(대조).
cat > "$TMP/ss.sh" <<'EOS'
printf 'dev tun\nremote 192.0.2.1 1194 udp\nifconfig 10.99.0.2 10.99.0.1\nroute 10.99.1.0 255.255.255.0\nroute-up "/bin/touch /tmp/pwned"\n' > /tmp/c.ovpn
timeout 6 openvpn --config /tmp/c.ovpn --dev tun0 --dev-type tun --script-security "$1" --verb 3 >/tmp/log 2>&1
[ -e /tmp/pwned ] && echo RAN || echo NOT-RAN
EOS
r1=$(drun --rm --name vpn-proxy-test-ss1 --tmpfs /tmp --entrypoint sh -v "$TMP/ss.sh:/ss.sh:ro" $IMAGE /ss.sh 1 2>&1 | tail -1)
r2=$(drun --rm --name vpn-proxy-test-ss2 --tmpfs /tmp --entrypoint sh -v "$TMP/ss.sh:/ss.sh:ro" $IMAGE /ss.sh 2 2>&1 | tail -1)
if [ "$r1" = "NOT-RAN" ] && [ "$r2" = "RAN" ]; then ok "route-up 스크립트: script-security 1 에서는 실행 안 됨(2 에서는 실행됨 — 대조)"; else bad "script-security 시험: 1→$r1, 2→$r2"; fi

echo "5) 서버가 내려 준 DNS 고르기 (로그 따라가기 함수)"
cat > "$TMP/unit.sh" <<'EOS'
NAME=t; STATE_DIR=/tmp/st; SOCKS_PORT=1080; RESOLV_CONF=/tmp/resolv; FIXED_DNS=""; FILE_DNS="10.9.9.9"; FILE_DOMAINS=""
mkdir -p $STATE_DIR; echo 0 > $STATE_DIR/gen; : > $RESOLV_CONF
. /usr/local/lib/vpn-lib.sh
set_state connecting x
# 접속 완료 문구가 서버가 내려 준 글 안에 섞여 있어도 '올라옴'으로 보지 않는다.
printf '%s\n' "2026-01-02 03:04:05 PUSH: Received control message: 'PUSH_REPLY,echo Initialization Sequence Completed,dhcp-option DNS 10.8.0.1'" | watch_log >/dev/null
echo "A=$(cat $STATE_DIR/state)"
rm -f $STATE_DIR/pushed-dns
printf '%s\n' \
  "2026-01-02 03:04:05 PUSH: Received control message: 'PUSH_REPLY,redirect-gateway def1,dhcp-option DNS 10.8.0.1,dhcp-option DNS 127.0.0.11,dhcp-option DNS 1.1.1.1;reboot,dhcp-option DNS \$(reboot),dhcp-option DOMAIN corp.internal,dhcp-option DOMAIN bad;name,route 10.8.0.1,ping 10'" \
  "2026-01-02 03:04:05 Initialization Sequence Completed" | watch_log >/dev/null
echo "B=$(cat $STATE_DIR/state) gen=$(cat $STATE_DIR/gen)"
echo "C=$(tr '\n' '|' < $RESOLV_CONF)"
echo "D=$(cat $STATE_DIR/status.json)"
printf '%s\n' "2026-01-02 03:04:05 SIGUSR1[soft,ping-restart] received, process restarting" | watch_log >/dev/null
echo "E=$(cat $STATE_DIR/state) dns=$(grep -c nameserver $RESOLV_CONF)"
# 지정한 DNS(VPN_DNS)가 있으면 서버가 내려 준 것보다 먼저다.
FIXED_DNS=" 10.7.7.7"
printf '%s\n' "2026-01-02 03:04:05 PUSH: Received control message: 'PUSH_REPLY,dhcp-option DNS 10.8.0.1'" "2026-01-02 03:04:05 Initialization Sequence Completed" | watch_log >/dev/null
echo "F=$(tr '\n' '|' < $RESOLV_CONF)"
# L1: 줄 전체 모양이 아니면 반응하지 않는다. 상대가 정한 글(인증서 이름)이 로그 줄 중간에 찍히는 경우.
FIXED_DNS=""; FILE_DNS=""; echo 0 > $STATE_DIR/gen; rm -f $STATE_DIR/pushed-dns $STATE_DIR/pushed-domains; : > $RESOLV_CONF
set_state connecting x
printf '%s\n' \
  "T VERIFY ERROR: depth=0, error=self-signed certificate: CN=Initialization Sequence Completed, serial=1" \
  "T VERIFY ERROR: depth=0, error=x: O=PUSH: Received control message:, CN=PUSH_REPLY,dhcp-option DNS 10.66.66.66,x" \
  "T VERIFY OK: depth=0, CN=x PUSH: Received control message: 'PUSH_REPLY,dhcp-option DNS 10.66.66.67'" \
  "T Initialization Sequence Completed " \
  "T Initialization Sequence Completed With Errors" \
  " T Initialization Sequence Completed" \
  "Initialization Sequence Completed" \
  "t Initialization Sequence Completed" \
  "T  Initialization Sequence Completed" | sed 's/^T /2026-01-02 03:04:05 /; s/^ T / 2026-01-02 03:04:05 /' | watch_log >/dev/null
echo "G=$(cat $STATE_DIR/state) gen=$(cat $STATE_DIR/gen) pushed=$(cat $STATE_DIR/pushed-dns 2>/dev/null | tr '\n' ' ')"
set_state up x
printf '%s\n' \
  "T VERIFY ERROR: depth=0, error=x: CN=process restarting" \
  "T VERIFY ERROR: CN=SIGUSR1[soft,x] received, process restarting" \
  "T SIGUSR1[soft,x] received, process restarting now" \
  "T SIG x received, process exiting" \
  "T SIGcn=a b[soft] received, process restarting" | sed 's/^T /2026-01-02 03:04:05 /' | watch_log >/dev/null
echo "H=$(cat $STATE_DIR/state)"
printf '%s\n' "2026-01-02 03:04:05 SIGTERM[hard,] received, process exiting" | watch_log >/dev/null
echo "I=$(cat $STATE_DIR/state)"
# L2: 쉼표로 나눈 조각의 '*' 가 현재 폴더의 파일 이름으로 바뀌지 않는다.
mkdir -p /tmp/glob; cd /tmp/glob; touch 'dhcp-option DNS 9.9.9.9' a b
rm -f $STATE_DIR/pushed-dns
printf '%s\n' "2026-01-02 03:04:05 PUSH: Received control message: 'PUSH_REPLY,dhcp-option DNS *,*,dhcp-option DNS 10.8.0.2'" | watch_log >/dev/null
echo "J=$(tr '\n' ' ' < $STATE_DIR/pushed-dns)"
case $- in *f*) echo "K=noglob-left-on" ;; *) echo "K=glob-restored" ;; esac
EOS
u=$($DOCKER run --rm --name vpn-proxy-test-unit --tmpfs /tmp --entrypoint sh -v "$TMP/unit.sh:/unit.sh:ro" $IMAGE /unit.sh 2>&1)
chk() { if echo "$u" | grep -qxF "$1"; then ok "$2"; else bad "$2 → $(echo "$u" | tr '\n' ' ')"; fi; }
chk "A=connecting" "서버가 내려 준 글 속의 '접속 완료' 문구로는 올라옴이 되지 않음"
chk "B=up gen=1" "접속 완료 줄에서 상태 up, 세대 1"
chk "C=nameserver 10.8.0.1|nameserver 10.9.9.9|search corp.internal|" "내려 준 DNS 중 형식이 맞는 것만(127.0.0.11·명령 섞인 값 제외), ovpn 의 DNS 는 그 뒤"
if echo "$u" | grep -q '^D={"name":"t","state":"up","since":"[0-9T:Z-]*","gen":1,"dns":\["10.8.0.1","10.9.9.9"\],"socks_port":1080,"message":"터널 연결됨"}$'; then ok "status.json 한 줄 JSON(name·state·since·gen·dns·socks_port·message)"; else bad "status.json 형식: $(echo "$u" | grep '^D=')"; fi
chk "E=reconnecting dns=0" "재시작 줄에서 상태 reconnecting, DNS 비움"
chk "F=nameserver 10.7.7.7|" "VPN_DNS 가 있으면 그것만 씀"
chk "G=connecting gen=0 pushed=" "L1: 인증서 이름 등 줄 중간의 '접속 완료'·PUSH 문구, 모양이 다른 줄로는 상태·DNS 가 바뀌지 않음"
chk "H=up" "L1: 줄 중간의 '재시작' 문구, 모양이 다른 재시작 줄로는 내려감 처리가 되지 않음"
chk "I=reconnecting" "L1: 실제 종료 줄(SIGTERM[hard,] received, process exiting)은 내려감으로 처리"
chk "J=10.8.0.2 " "L2: PUSH_REPLY 의 '*' 가 파일 이름으로 바뀌지 않음"
chk "K=glob-restored" "L2: 이름 확장은 반복문 뒤에 다시 켜짐"

echo "6) 시험용 터널(상대 컨테이너와 1:1, 암호·비밀값 없음)"
$DOCKER network create "$NET" >/dev/null
# 상대: 같은 이미지의 openvpn 을 직접 띄운다. 터널 주소 10.99.0.1, 그 주소에서 작은 HTTP 응답을 준다.
$DOCKER run -d --name vpn-proxy-test-peer --network "$NET" --cap-drop ALL --cap-add NET_ADMIN --device /dev/net/tun \
  --entrypoint openvpn $IMAGE --dev tun0 --ifconfig 10.99.0.1 10.99.0.2 --proto udp --port 1194 --float --ping 1 --verb 3 >/dev/null
peer_ip=$($DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' vpn-proxy-test-peer)
$DOCKER exec -d vpn-proxy-test-peer sh -c 'while :; do printf "HTTP/1.0 200 OK\r\nContent-Length: 10\r\n\r\nvia-tunnel" | nc -l -p 8099 -s 10.99.0.1; done'
printf 'dev tun\nproto udp\nremote %s 1194\nifconfig 10.99.0.2 10.99.0.1\nping 1\nping-restart 4\ndhcp-option DNS 10.99.0.53\nuser nobody\ngroup nobody\nsetenv opt user nobody\n' "$peer_ip" > "$TMP/client/client.ovpn"
drun -d --name vpn-proxy-test-client --network "$NET" -p 127.0.0.1::1080 -v "$TMP/client:/vpn:ro" $IMAGE >/dev/null

if wait_state up 25 && sleep 3 && in_c pidof sockd >/dev/null 2>&1; then
  ok "터널이 올라온 뒤 SOCKS5 시작(state=up)"
  if in_c cat /run/vpn/status.json | grep -q '"state":"up".*"dns":\["10.99.0.53"\]'; then ok "status.json 에 state=up 과 DNS"; else bad "status.json: $(in_c cat /run/vpn/status.json)"; fi
  # ovpn 에 user·group 줄이 있어도(8·9·10번째 줄) 사본에서 빼고 뜬다. 원본은 그대로다.
  clog=$($DOCKER logs vpn-proxy-test-client 2>&1)
  if [ "$(echo "$clog" | grep -c 'user/group 은 컨테이너 안에서 쓰지 않아 뺐습니다')" = "3" ] && echo "$clog" | grep -q '줄 8: user/group' && echo "$clog" | grep -q '줄 10: user/group'; then ok "user·group 줄(setenv opt 포함) 3개를 뺐다고 줄 번호와 함께 안내"; else bad "user/group 안내: $(echo "$clog" | grep 'user/group')"; fi
  if ! in_c grep -Eq '^[[:space:]]*(setenv[[:space:]]+opt[[:space:]]+)*(user|group)([[:space:]]|$)' /run/vpn/client.ovpn && [ "$(in_c grep -c '^user nobody$' /vpn/client.ovpn)" = "1" ] && [ "$(in_c sh -c 'wc -l < /run/vpn/client.ovpn')" = "$(in_c sh -c 'wc -l < /vpn/client.ovpn')" ]; then ok "사본에는 user·group 이 없고 줄 수는 같으며 원본 파일은 그대로"; else bad "사본/원본: $(in_c grep -n 'user\|group' /run/vpn/client.ovpn /vpn/client.ovpn)"; fi
  if [ "$(in_c sh -c 'stat -c %u /proc/$(cat /run/vpn/openvpn.pid)')" = "0" ] && in_c sh -c 'kill -0 $(cat /run/vpn/openvpn.pid)'; then ok "OpenVPN 은 root 로 남아 entrypoint 가 살아 있는지 확인·종료할 수 있음"; else bad "OpenVPN 사용자: $(in_c sh -c 'stat -c %u /proc/$(cat /run/vpn/openvpn.pid)')"; fi
  cmdline=$(in_c sh -c 'tr "\0" " " < /proc/$(cat /run/vpn/openvpn.pid)/cmdline')
  case "$cmdline" in *"--script-security 1 "*) ok "실행 중인 OpenVPN 은 --script-security 1" ;; *) bad "OpenVPN 옵션: $cmdline" ;; esac

  rules=$(in_c iptables -S 2>&1)
  if echo "$rules" | grep -q -- "-P OUTPUT DROP" && echo "$rules" | grep -q -- "-P INPUT DROP" && echo "$rules" | grep -q -- "-P FORWARD DROP"; then ok "방화벽 기본 정책 DROP"; else bad "기본 정책: $rules"; fi
  if echo "$rules" | grep -q -- "-A OUTPUT -d $peer_ip/32 ! -o tun+ -p udp -m udp --dport 1194 -j ACCEPT" && [ "$(echo "$rules" | grep -c -- '! -o tun+')" = "1" ]; then ok "터널 밖 허용은 VPN 서버 주소·포트 한 줄뿐"; else bad "서버 허용 규칙: $rules"; fi
  if in_c sh -c 'nc -w 3 1.1.1.1 443 </dev/null' >/dev/null 2>&1; then bad "터널 밖 TCP 가 나감"; else ok "컨테이너 안에서 터널 밖 TCP 차단(1.1.1.1:443)"; fi
  if in_c sh -c 'dig +time=2 +tries=1 @1.1.1.1 example.com' >/dev/null 2>&1; then bad "터널 밖 DNS 가 나감"; else ok "컨테이너 안에서 터널 밖 DNS 차단(1.1.1.1:53)"; fi
  if [ "$(in_c cat /etc/resolv.conf)" = "nameserver 10.99.0.53" ]; then ok "DNS 가 터널 안 주소로 바뀜"; else bad "resolv.conf: $(in_c cat /etc/resolv.conf)"; fi

  port=$($DOCKER port vpn-proxy-test-client 1080/tcp)
  case "$port" in 127.0.0.1:*) ok "호스트 바인드 $port" ;; *) bad "호스트 바인드가 127.0.0.1 이 아님: $port" ;; esac
  hp=${port##*:}
  sx() { curl -s --max-time 6 --noproxy '' --socks5-hostname "127.0.0.1:$hp" "$@" 2>/dev/null; }
  body=$(sx http://10.99.0.1:8099/); crc=$?
  if [ "$body" = "via-tunnel" ]; then ok "SOCKS → 터널 → 상대 쪽 서버 응답 수신"; else bad "터널 안 중계 실패: rc=$crc body=$body"; fi
  code=$(sx -o /dev/null -w '%{http_code}' http://1.1.1.1/); crc=$?
  if [ $crc -ne 0 ] && [ "$code" = "000" ]; then ok "SOCKS 로 터널 밖 주소 접속 실패(curl 종료 코드 $crc)"; else bad "SOCKS 로 터널 밖 주소가 열림: http=$code rc=$crc"; fi
  sx -o /dev/null http://127.0.0.11:53/; crc=$?
  if [ $crc -eq 97 ] && $DOCKER logs vpn-proxy-test-client 2>&1 | grep -q "block(1): tcp/connect.*127.0.0.11.53"; then ok "SOCKS 로 127.0.0.11:53(Docker DNS) 차단(F2)"; else bad "127.0.0.11 로 중계됨: rc=$crc"; fi
  sx -o /dev/null http://127.0.0.1:1080/; crc=$?
  if [ $crc -eq 97 ]; then ok "SOCKS 로 127.0.0.1 차단(F2)"; else bad "127.0.0.1 로 중계됨: rc=$crc"; fi

  caps=$(in_c grep CapEff /proc/1/status | tr -d '[:space:]')
  if [ "$caps" = "CapEff:00000000000010c0" ]; then ok "권한은 NET_ADMIN·SETUID·SETGID 뿐(NET_RAW 없음)"; else bad "권한 값이 다름: $caps"; fi
  if in_c ping -c 1 -W 2 10.99.0.1 >/dev/null 2>&1; then ok "NET_RAW 없이 터널 건너편으로 ping"; else bad "ping 이 안 됨"; fi
  if [ "$(in_c stat -c %a /run/vpn)" = "755" ]; then ok "/run/vpn 은 root 만 씀(755)"; else bad "/run/vpn 권한이 다름"; fi
  if $DOCKER exec -u nobody vpn-proxy-test-client sh -c 'echo up > /run/vpn/state || echo 1 > /run/vpn/gen || echo x >> /run/vpn/sockd.conf || touch /run/vpn/new || cat /run/vpn/client.ovpn' >/dev/null 2>&1; then bad "nobody 가 /run/vpn 을 고치거나 ovpn 을 읽을 수 있음"; else ok "nobody 는 state·gen·sockd.conf 를 못 고치고 ovpn 사본도 못 읽음"; fi

  # 재연결 신호: 바로 SOCKS 가 꺼지고 DNS 가 비워져야 한다.
  in_c pkill -USR1 openvpn
  sleep 0.5
  s1=$(in_c cat /run/vpn/state); p1=$(in_c pidof sockd 2>/dev/null); d1=$(in_c grep -c nameserver /etc/resolv.conf)
  if [ "$s1" = "reconnecting" ] && [ -z "$p1" ] && [ "$d1" = "0" ]; then ok "재연결 신호 0.5초 뒤: state=reconnecting, SOCKS 없음, DNS 비움"; else bad "재연결 직후: state=$s1 sockd=[$p1] dns=$d1"; fi
  if wait_state up 20 && sleep 3 && in_c pidof sockd >/dev/null 2>&1 && [ "$(sx http://10.99.0.1:8099/)" = "via-tunnel" ]; then ok "다시 연결되면 SOCKS 가 다시 켜지고 중계됨"; else bad "재연결 뒤 복구 안 됨: state=$(in_c cat /run/vpn/state)"; fi

  # 터널 끊김: 상대를 멈추면 응답이 없어 OpenVPN 이 재시작에 들어가고 SOCKS 가 닫혀야 한다.
  $DOCKER stop -t 1 vpn-proxy-test-peer >/dev/null
  if wait_state reconnecting 20; then
    sleep 1
    if in_c pidof sockd >/dev/null 2>&1; then bad "터널이 끊겼는데 SOCKS 가 남아 있음"; else ok "상대가 사라짐 → state=reconnecting, SOCKS 종료(kill-switch)"; fi
    sx -o /dev/null http://10.99.0.1:8099/; crc=$?
    if [ $crc -ne 0 ]; then ok "터널이 끊긴 동안 SOCKS 요청 실패(curl 종료 코드 $crc)"; else bad "터널이 끊겼는데 SOCKS 가 응답함"; fi
  else
    bad "상대가 사라졌는데 state 가 reconnecting 이 되지 않음: $(in_c cat /run/vpn/state)"
  fi

  # OpenVPN 이 죽으면 SOCKS 도 닫히고 컨테이너가 끝나야 한다.
  in_c pkill -9 openvpn >/dev/null 2>&1
  sleep 5
  if $DOCKER exec vpn-proxy-test-client pidof sockd >/dev/null 2>&1; then bad "OpenVPN 이 죽었는데 SOCKS 가 남아 있음"; else ok "OpenVPN 종료 → SOCKS 도 종료"; fi
else
  bad "터널이 올라오지 않음: $($DOCKER logs vpn-proxy-test-client 2>&1 | tail -15)"
fi

echo "7) DNS 설정을 못 쓰면 시작하지 않는다 (F3)"
printf 'dev tun\nremote 192.0.2.1 1194 udp\nifconfig 10.99.0.2 10.99.0.1\n' > "$TMP/ro/client.ovpn"
out=$(drun --rm --name vpn-proxy-test-ro --read-only -e VPN_FAIL_SLEEP=0 -v "$TMP/ro:/vpn:ro" $IMAGE 2>&1); rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -q "resolv.conf 를 쓸 수 없습니다" && ! echo "$out" | grep -q "SOCKS5 시작"; then ok "읽기 전용 파일 시스템 → SOCKS 를 켜지 않고 종료 코드 $rc"; else bad "읽기 전용인데 진행됨: rc=$rc out=$out"; fi

echo
if [ $fail -eq 0 ]; then echo "결과: 모두 통과"; else echo "결과: 실패 있음"; fi
exit $fail
