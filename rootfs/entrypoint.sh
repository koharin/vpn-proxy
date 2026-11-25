#!/bin/sh
# 순서: 설정 확인 → ovpn 검사 → VPN 서버 주소 고정 → 방화벽(kill-switch) → OpenVPN → 터널 확인 → SOCKS5.
# 터널이 내려가면 SOCKS5 를 끄고, 다시 올라오면 켠다. OpenVPN 이 죽으면 컨테이너가 끝난다
# (compose 의 restart 가 다시 띄운다).
set -eu

NAME="${VPN_NAME:-vpn}"
VPN_DIR="${VPN_DIR:-/vpn}"
SOCKS_PORT="${SOCKS_PORT:-1080}"
CONNECT_TIMEOUT="${VPN_CONNECT_TIMEOUT:-60}"
STATE_DIR=/run/vpn
STATE="$STATE_DIR/state"   # 내용이 'up' 이면 터널이 올라와 있다

log() { echo "[$NAME] $*"; }
die() {
  echo "[$NAME] 오류: $*" >&2
  # 바로 끝나면 restart 정책이 1초마다 다시 띄운다. 잠깐 쉬어 로그가 넘치지 않게 한다.
  sleep "${VPN_FAIL_SLEEP:-10}"
  exit 1
}

is_number() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }
is_ipv4() { echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }
tunnel_up() { [ "$(cat "$STATE" 2>/dev/null)" = "up" ] && ip link show tun0 >/dev/null 2>&1; }

is_number "$SOCKS_PORT" || die "SOCKS_PORT 가 숫자가 아닙니다: $SOCKS_PORT"
is_number "$CONNECT_TIMEOUT" || die "VPN_CONNECT_TIMEOUT 이 숫자가 아닙니다: $CONNECT_TIMEOUT"

# ── 1. 설정 파일 ────────────────────────────────────────────────────────────
[ -d "$VPN_DIR" ] || die "$VPN_DIR 폴더가 없습니다. compose 의 volumes 를 확인하세요."

if [ -n "${VPN_CONFIG:-}" ]; then
  SRC_CONFIG="$VPN_DIR/$VPN_CONFIG"
  [ -f "$SRC_CONFIG" ] || die "ovpn 파일이 없습니다: $SRC_CONFIG (호스트의 ovpn/<이름>/ 폴더에 넣으세요)"
else
  count=$(find "$VPN_DIR" -maxdepth 1 -type f -name '*.ovpn' | wc -l | tr -d ' ')
  [ "$count" -ge 1 ] || die "ovpn 파일이 없습니다. 호스트의 ovpn/<이름>/ 폴더에 .ovpn 파일 1개를 넣으세요. (컨테이너 안 경로: $VPN_DIR)"
  [ "$count" -eq 1 ] || die ".ovpn 파일이 $count 개입니다. 1개만 두거나 VPN_CONFIG 로 파일 이름을 지정하세요."
  SRC_CONFIG=$(find "$VPN_DIR" -maxdepth 1 -type f -name '*.ovpn')
fi
[ -s "$SRC_CONFIG" ] || die "ovpn 파일이 비어 있습니다: $SRC_CONFIG"

# 상태 폴더는 root 만 쓴다. 터널 표시(state)와 세대(gen) 두 파일만 누구나 쓸 수 있게 둔다 —
# ovpn 에 'user nobody' 가 있으면 재연결 때 훅 스크립트가 nobody 로 돌기 때문이다.
mkdir -p "$STATE_DIR"
chmod 0755 "$STATE_DIR"
echo down > "$STATE"
echo 0 > "$STATE_DIR/gen"
chmod 0666 "$STATE" "$STATE_DIR/gen"

# 검사한 내용 그대로 실행하도록 사본을 만들어 그 사본으로 검사하고 실행한다.
CONFIG="$STATE_DIR/client.ovpn"
cp "$SRC_CONFIG" "$CONFIG"
chmod 0600 "$CONFIG"

# ── 2. ovpn 검사: 명령을 실행하게 하는 지시는 받지 않는다 ──────────────────
# ovpn 을 만든 쪽이 컨테이너 안에서 명령을 실행하거나 방화벽을 끄지 못하게 한다.
# 줄마다 첫 단어(지시 이름)만 본다. 인증서 같은 인라인 블록(<ca> … </ca>)의 안쪽은 건너뛴다.
if ! found=$(awk '
  BEGIN {
    n = split("up down route-up route-pre-down ipchange tls-verify tls-export-cert auth-user-pass-verify " \
              "client-connect client-disconnect client-crresponse learn-address plugin script-security " \
              "iproute config management pkcs11-providers pkcs11-id-management " \
              "log log-append status writepid daemon chroot cd tmp-dir", a, " ")
    for (i = 1; i <= n; i++) bad[a[i]] = 1
    n = split("ca cert key extra-certs dh pkcs12 secret tls-auth tls-crypt tls-crypt-v2 crl-verify " \
              "auth-user-pass http-proxy-user-pass peer-fingerprint verify-hash", b, " ")
    for (i = 1; i <= n; i++) inline_ok[b[i]] = 1
    n = split("opt CLIENT_CERT FRIENDLY_NAME GENERIC_CONFIG FORWARD_COMPATIBLE PUSH_PEER_INFO", c, " ")
    for (i = 1; i <= n; i++) env_ok[c[i]] = 1
    skip = ""; rc = 0
  }
  function report(why) { printf "  %d번째 줄: %s\n", NR, why; rc = 1 }
  {
    sub(/\r$/, "")
    line = $0
    sub(/^[ \t]+/, "", line)
    if (skip != "") {                       # 인라인 블록 안: 닫는 표시만 찾는다
      if (tolower(line) ~ ("^</" skip ">[ \t]*$")) skip = ""
      next
    }
    if (line == "" || line ~ /^[#;]/) next
    word = line
    sub(/[ \t].*$/, "", word)
    if (word ~ /["\047\\]/) { report("지시 이름에 따옴표나 역슬래시가 있습니다"); next }
    low = tolower(word)
    if (low ~ /^<\/connection>$/ || low ~ /^<connection>$/) next
    if (low ~ /^<.*>$/) {
      tag = low; sub(/^</, "", tag); sub(/>$/, "", tag)
      if (!(tag in inline_ok)) { report("받지 않는 블록 <" tag ">"); next }
      skip = tag
      next
    }
    sub(/^--/, "", low)
    if (low in bad || low ~ /^management-/) { report("받지 않는 지시 \"" low "\""); next }
    if (low == "setenv") {
      split(line, f, /[ \t]+/)
      if (!(f[2] in env_ok)) report("받지 않는 지시 \"setenv " f[2] "\"")
    }
  }
  END {
    if (skip != "") { printf "  블록 <%s> 가 닫히지 않았습니다\n", skip; rc = 1 }
    exit rc
  }' "$CONFIG"); then
  echo "[$NAME] ovpn 에 받지 않는 지시가 있습니다:" >&2
  echo "$found" >&2
  die "이 ovpn 으로는 시작하지 않습니다. 명령을 실행하거나 파일을 쓰게 하는 지시는 받지 않습니다. 위 줄을 지우거나, 출처를 믿을 수 없으면 이 ovpn 을 쓰지 마세요."
fi

AUTH=""
if [ -n "${VPN_AUTH_FILE:-}" ]; then
  AUTH="$VPN_DIR/$VPN_AUTH_FILE"
  [ -f "$AUTH" ] || die "VPN_AUTH_FILE 로 지정한 파일이 없습니다: $AUTH"
elif [ -f "$VPN_DIR/auth.txt" ]; then
  AUTH="$VPN_DIR/auth.txt"
elif grep -Eq '^[[:space:]]*auth-user-pass[[:space:]]*$' "$CONFIG"; then
  die "이 ovpn 은 아이디·비밀번호가 필요합니다(auth-user-pass). ovpn 과 같은 폴더에 auth.txt(1줄: 아이디, 2줄: 비밀번호)를 넣으세요."
fi

[ -c /dev/net/tun ] || die "/dev/net/tun 이 없습니다. compose 의 devices 와 cap_add(NET_ADMIN)를 확인하세요."

# ── 3. VPN 서버 주소(remote) 읽기 ──────────────────────────────────────────
# 방화벽은 이 주소·포트로 가는 것만 터널 밖으로 내보낸다.
norm_proto() {
  case "$1" in
    tcp*) echo tcp ;;
    udp*) echo udp ;;
    *) echo "" ;;
  esac
}
DEF_PORT=$(awk '$1=="port"{p=$2} END{print p}' "$CONFIG")
DEF_PORT="${DEF_PORT:-1194}"
DEF_PROTO=$(norm_proto "$(awk '$1=="proto"{p=$2} END{print p}' "$CONFIG")")
DEF_PROTO="${DEF_PROTO:-udp}"

REMOTES="$STATE_DIR/remotes"
awk -v dp="$DEF_PORT" -v dpr="$DEF_PROTO" '
  $1=="remote" && $2!="" {
    port = ($3!="" ? $3 : dp); proto = ($4!="" ? $4 : dpr); print $2, port, proto
  }' "$CONFIG" > "$REMOTES"
[ -s "$REMOTES" ] || die "ovpn 에 remote 줄이 없습니다."

# OpenVPN 은 훅 스크립트에 컨테이너 환경변수를 넘기지 않는다. 필요한 값은 파일로 넘긴다.
if [ -n "${VPN_DNS:-}" ]; then
  echo "$VPN_DNS" | grep -Eq '^[0-9A-Fa-f:., ]+$' || die "VPN_DNS 형식이 이상합니다(IP 주소, 쉼표로 여러 개)."
fi
printf '%s\n' "${VPN_DNS:-}" > "$STATE_DIR/dns"
printf '%s\n' "$NAME" > "$STATE_DIR/name"

# 이름으로 적힌 서버는 지금(방화벽 전) 주소를 찾아 /etc/hosts 에 고정한다.
# 재연결 때 DNS 없이도 같은 주소로 붙는다.
ALLOW="$STATE_DIR/allow"
: > "$ALLOW"
while read -r host port proto; do
  proto=$(norm_proto "$proto")
  [ -n "$proto" ] || die "remote 의 프로토콜을 알 수 없습니다: $host"
  is_number "$port" || die "remote 의 포트가 숫자가 아닙니다: $host $port"
  if is_ipv4 "$host"; then
    echo "$host $port $proto" >> "$ALLOW"
  else
    echo "$host" | grep -Eq '^[A-Za-z0-9._-]+$' || die "remote 호스트 이름 형식이 이상합니다."
    ips=$(dig +short +time=3 +tries=2 A "$host" 2>/dev/null | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || true)
    if [ -z "$ips" ]; then
      log "경고: VPN 서버 이름을 찾지 못했습니다: $host"
      continue
    fi
    for ip in $ips; do
      echo "$ip $port $proto" >> "$ALLOW"
      grep -q "^$ip $host\$" /etc/hosts || echo "$ip $host" >> /etc/hosts
    done
  fi
done < "$REMOTES"
[ -s "$ALLOW" ] || die "VPN 서버 주소를 하나도 찾지 못했습니다(DNS 또는 네트워크 확인)."

# 서버 주소를 고정했으니 터널 밖 DNS(Docker DNS)는 더 쓰지 않는다. 지금 비워 두고,
# 터널이 올라오면 vpn-up.sh 가 VPN 의 DNS 를 적는다. 비우지 못하면 이름이 터널 밖으로
# 샐 수 있으므로 시작하지 않는다.
if ! echo "# 터널이 올라오기 전: 이름 해석을 하지 않는다" > /etc/resolv.conf 2>/dev/null; then
  die "/etc/resolv.conf 를 쓸 수 없습니다. 이름 해석이 터널 밖으로 샐 수 있어 시작하지 않습니다(루트 파일 시스템을 읽기 전용으로 띄우지 마세요)."
fi

# ── 4. 방화벽(kill-switch) ─────────────────────────────────────────────────
# 나가는 길: 터널(tun), VPN 서버 주소, 이미 들어온 SOCKS 연결의 응답뿐.
# 들어오는 길: 터널이 아닌 쪽에서 오는 SOCKS 포트뿐(VPN 쪽 상대는 이 프록시를 못 쓴다).
iptables -P INPUT DROP || die "방화벽을 설정하지 못했습니다(NET_ADMIN 권한 필요). 방화벽 없이 시작하지 않습니다."
iptables -P OUTPUT DROP
iptables -P FORWARD DROP
iptables -F INPUT
iptables -F OUTPUT
iptables -F FORWARD
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT ! -i tun+ -p tcp --dport "$SOCKS_PORT" -j ACCEPT
iptables -A OUTPUT -o tun+ -j ACCEPT
while read -r ip port proto; do
  iptables -A OUTPUT ! -o tun+ -d "$ip" -p "$proto" --dport "$port" -j ACCEPT
done < "$ALLOW"

# IPv6 는 전부 막는다. ip6tables 를 못 쓰면 IPv6 가 꺼져 있어야 한다.
if ip6tables -P INPUT DROP 2>/dev/null && ip6tables -P OUTPUT DROP 2>/dev/null && ip6tables -P FORWARD DROP 2>/dev/null; then
  ip6tables -F INPUT; ip6tables -F OUTPUT; ip6tables -F FORWARD
  ip6tables -A INPUT -i lo -j ACCEPT
  ip6tables -A OUTPUT -o lo -j ACCEPT
elif [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo 1)" != "1" ]; then
  die "IPv6 방화벽을 설정하지 못했고 IPv6 가 켜져 있습니다. compose 의 sysctls 로 IPv6 를 끄세요."
fi
log "방화벽 적용: 터널과 VPN 서버 $(wc -l < "$ALLOW" | tr -d ' ')곳만 허용"

# ── 5. OpenVPN ─────────────────────────────────────────────────────────────
OVPN_PID=""
SOCKS_PID=""
# sockd 는 자식 프로세스를 여럿 둔다. 정상 종료를 2초 기다리고, 남으면 전부 강제로 끝낸다.
kill_socks() {
  [ -n "$SOCKS_PID" ] && kill "$SOCKS_PID" 2>/dev/null || true
  n=0
  while pidof sockd >/dev/null 2>&1 && [ "$n" -lt 4 ]; do
    sleep 0.5
    n=$((n + 1))
  done
  pidof sockd >/dev/null 2>&1 && kill -9 $(pidof sockd) 2>/dev/null || true
  SOCKS_PID=""
}
stop_all() {
  kill_socks
  [ -n "$OVPN_PID" ] && kill "$OVPN_PID" 2>/dev/null || true
}
trap 'stop_all; exit 0' TERM INT

# script-security 는 여기(명령행)에서만 정한다. 2 는 이 이미지의 vpn-up.sh·vpn-down.sh 를 부르기 위한 값이다.
# ovpn 안의 스크립트 지시는 2번 단계에서 이미 걸러졌고, 명령행의 값이 ovpn 뒤에 와서 이긴다.
set -- --config "$CONFIG" --cd "$VPN_DIR" --dev tun0 --dev-type tun \
  --script-security 2 --up /usr/local/bin/vpn-up.sh --down /usr/local/bin/vpn-down.sh --up-restart \
  --auth-nocache --verb "${VPN_VERB:-3}"
[ -n "$AUTH" ] && set -- "$@" --auth-user-pass "$AUTH"

log "OpenVPN 시작: $(basename "$SRC_CONFIG")"
openvpn "$@" &
OVPN_PID=$!

waited=0
while ! tunnel_up; do
  if ! kill -0 "$OVPN_PID" 2>/dev/null; then
    die "OpenVPN 이 연결 전에 끝났습니다. 위 로그를 확인하세요(인증서, 아이디·비밀번호, 서버 주소)."
  fi
  if [ "$waited" -ge "$CONNECT_TIMEOUT" ]; then
    stop_all
    die "${CONNECT_TIMEOUT}초 안에 VPN 이 연결되지 않았습니다."
  fi
  sleep 1
  waited=$((waited + 1))
done

# ── 6. SOCKS5: 터널이 올라와 있을 때만 켠다 ───────────────────────────────
# 나가는 연결은 터널 주소(tun0)에서만 출발한다. 인증은 없다 — 포트가 Mac 의 127.0.0.1 에만 열리기 때문이다.
# 컨테이너 자신(127.0.0.0/8)으로 가는 요청은 막는다. Docker 내장 DNS(127.0.0.11)가 여기에 있어,
# 열어 두면 SOCKS 로 터널 밖에 이름을 물을 수 있다.
start_socks() {
  cat > "$STATE_DIR/sockd.conf" <<CONF
logoutput: stderr
internal: 0.0.0.0 port = $SOCKS_PORT
external: tun0
clientmethod: none
socksmethod: none
user.privileged: root
user.unprivileged: nobody
client pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  log: error
}
socks block {
  from: 0.0.0.0/0 to: 127.0.0.0/8
  log: connect
}
socks pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  command: connect
  log: error
}
CONF
  sockd -f "$STATE_DIR/sockd.conf" -p "$STATE_DIR/sockd.pid" &
  SOCKS_PID=$!
  # 바로 죽으면(설정 오류 등) 조용히 반복하지 않고 멈춘다.
  sleep 1
  if ! kill -0 "$SOCKS_PID" 2>/dev/null; then
    stop_all
    die "SOCKS5 서버(sockd)가 시작하자마자 끝났습니다. 위 로그를 확인하세요."
  fi
  log "SOCKS5 시작: 컨테이너 포트 $SOCKS_PORT (터널 연결됨)"
}
stop_socks() {
  [ -n "$SOCKS_PID" ] || return 0
  kill_socks
  log "SOCKS5 중단: 터널이 내려갔습니다"
}

# gen 은 터널이 올라올 때마다 1 씩 는다. 값이 바뀌었으면 그 사이에 터널이 한 번 내려갔던 것이므로
# (2초 간격 사이에 지나갔더라도) SOCKS5 를 새 터널 기준으로 다시 띄운다.
SOCKS_GEN=""
while kill -0 "$OVPN_PID" 2>/dev/null; do
  if tunnel_up; then
    gen=$(cat "$STATE_DIR/gen" 2>/dev/null || echo 0)
    if [ -n "$SOCKS_PID" ] && [ "$gen" != "$SOCKS_GEN" ]; then
      stop_socks
    fi
    if [ -z "$SOCKS_PID" ] || ! kill -0 "$SOCKS_PID" 2>/dev/null; then
      start_socks
      SOCKS_GEN="$gen"
    fi
  else
    stop_socks
  fi
  sleep 2
done

stop_all
die "OpenVPN 이 끝났습니다. SOCKS5 를 내리고 컨테이너를 종료합니다."
