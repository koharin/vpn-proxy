#!/bin/sh
# entrypoint.sh 가 불러 쓰는 함수: 상태 기록, DNS 적용, OpenVPN 로그 따라가기.
# 따로 둔 이유: 컨테이너를 띄우지 않고 이 함수들만 불러 시험할 수 있게 하기 위함이다(tests/smoke.sh).
#
# 쓰는 변수(부르는 쪽이 정한다)
#   NAME          로그·상태에 찍는 이름
#   STATE_DIR     상태 폴더(/run/vpn)
#   SOCKS_PORT    SOCKS5 포트
#   RESOLV_CONF   DNS 설정 파일(/etc/resolv.conf)
#   FIXED_DNS     VPN_DNS 로 지정한 주소(공백 구분). 있으면 이것만 쓴다
#   FILE_DNS, FILE_DOMAINS  ovpn 파일에 적힌 dhcp-option 값

log() { echo "[$NAME] $*"; }
is_ipv4() { echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }

# DNS 서버 주소로 쓸 수 있는 값인지. IPv4 만 받고, 컨테이너 자신(127.x)과 0.x 는 받지 않는다
# (127.0.0.11 은 Docker DNS 라 터널 밖으로 이름을 묻게 된다).
# 0 으로 시작하는 두 자리 이상 숫자(00.1.2.3, 010.0.0.1)도 받지 않는다 — 0.x 를 달리 적은 것이거나
# 8진수로 읽힐 수 있다(gui/vpnstore.py 의 dns_list 와 같은 기준).
ok_dns() {
  is_ipv4 "$1" || return 1
  case "$1" in 127.*|0.*|0[0-9]*|*.0[0-9]*) return 1 ;; esac
  return 0
}

# 상태를 바꾼다. $1 = 상태 단어, $2 = 사람이 읽을 한 줄(따옴표·역슬래시 없이).
set_state() {
  [ -d "$STATE_DIR" ] || return 0
  dns_json=$(awk '$1=="nameserver"{printf "%s\"%s\"", (n++ ? "," : ""), $2}' "$RESOLV_CONF" 2>/dev/null || true)
  printf '{"name":"%s","state":"%s","since":"%s","gen":%s,"dns":[%s],"socks_port":%s,"message":"%s"}\n' \
    "$NAME" "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(cat "$STATE_DIR/gen" 2>/dev/null || echo 0)" \
    "$dns_json" "$SOCKS_PORT" "$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n' ' ')" > "$STATE_DIR/status.json.tmp" 2>/dev/null &&
    mv "$STATE_DIR/status.json.tmp" "$STATE_DIR/status.json"
  echo "$1" > "$STATE_DIR/state"
}


# 터널이 올라왔을 때 DNS 를 정한다. 우선순위: VPN_DNS → 서버가 내려 준 것 → ovpn 에 적힌 것.
# 쓰고 나서 다시 읽어 확인한다. 못 썼으면 1 — 그때는 터널을 '올라옴'으로 표시하지 않는다.
apply_dns() {
  servers="$FIXED_DNS"
  domains=""
  if [ -z "$servers" ]; then
    servers="$(cat "$STATE_DIR/pushed-dns" 2>/dev/null || true) $FILE_DNS"
    domains="$(cat "$STATE_DIR/pushed-domains" 2>/dev/null || true) $FILE_DOMAINS"
  fi
  content=""
  valid=""
  for s in $servers; do
    ok_dns "$s" || continue
    case " $valid " in *" $s "*) continue ;; esac
    valid="$valid $s"
    content="${content}nameserver $s
"
  done
  if [ -n "$valid" ]; then
    clean=""
    for d in $domains; do
      echo "$d" | grep -Eq '^[A-Za-z0-9._-]+$' && clean="$clean $d"
    done
    [ -n "$clean" ] && content="${content}search$clean
"
  else
    content="# VPN DNS 없음: 이름 해석을 하지 않는다(VPN_DNS 로 지정)
"
  fi
  printf '%s' "$content" > "$RESOLV_CONF" 2>/dev/null
  [ "$(cat "$RESOLV_CONF" 2>/dev/null)" = "$(printf '%s' "$content")" ] || return 1
  if [ -n "$valid" ]; then
    log "DNS: 터널 안의 DNS 를 씁니다 ($(echo $valid))"
  else
    log "경고: VPN 이 DNS 를 알려 주지 않았습니다. 이름으로는 접속되지 않고 IP 주소로만 됩니다. 이름을 쓰려면 .env 의 VPN_x_DNS 에 내부 DNS 주소를 적으세요."
  fi
  return 0
}
clear_dns() { echo "# 터널이 내려감: 이름 해석을 하지 않는다" > "$RESOLV_CONF" 2>/dev/null || true; }

# OpenVPN 의 로그를 한 줄씩 읽어 그대로 내보내면서 터널 상태를 따라간다(훅 스크립트 대신).
# 줄 "전체"의 모양이 맞을 때만 반응한다. 줄은 "YYYY-MM-DD HH:MM:SS " 로 시작하고, 그 뒤가
#   접속 완료   정확히 "Initialization Sequence Completed"                → DNS 적용, gen+1, state=up
#   재시작·종료 정확히 "SIG…[…] received, process restarting|exiting"     → state=reconnecting, SOCKS5 즉시 종료, DNS 비움
#   내려 준 설정 "PUSH: Received control message: 'PUSH_REPLY," 로 시작    → DNS 주소만 골라 둔다
# 인 줄만 받는다. 줄 중간에 같은 글이 들어 있는 것은 무시한다 — 상대가 정한 글(인증서 이름 등)이
# 로그에 그대로 찍히므로, "어디에든 들어 있으면"으로 맞추면 인증되지 않은 상대가 상태를 바꿀 수 있다.
# 로그의 글은 판단에만 쓰고 명령으로 실행하지 않는다. 서버가 내려 준 값은 형식을 확인한 것만 쓴다.
PUSH_HEAD="PUSH: Received control message: 'PUSH_REPLY,"
watch_log() {
  while IFS= read -r line; do
    printf '%s\n' "$line"
    case "$line" in
      [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\ [0-9][0-9]:[0-9][0-9]:[0-9][0-9]\ *) msg="${line#??????????????????? }" ;;
      *) continue ;;
    esac
    case "$msg" in
      "$PUSH_HEAD"*)
        body="${msg#"$PUSH_HEAD"}"
        body="${body%\'}"
        # 쉼표로 나눌 때 '*' 같은 글자가 파일 이름으로 바뀌지 않게 이름 확장을 끈다.
        old_ifs="$IFS"; IFS=,
        set -f
        for item in $body; do
          case "$item" in
            "dhcp-option DNS "*) v="${item#dhcp-option DNS }"; ok_dns "$v" && echo "$v" >> "$STATE_DIR/pushed-dns" ;;
            "dhcp-option DOMAIN "*) v="${item#dhcp-option DOMAIN }"; echo "$v" | grep -Eq '^[A-Za-z0-9._-]+$' && echo "$v" >> "$STATE_DIR/pushed-domains" ;;
            "dhcp-option DOMAIN-SEARCH "*) v="${item#dhcp-option DOMAIN-SEARCH }"; echo "$v" | grep -Eq '^[A-Za-z0-9._-]+$' && echo "$v" >> "$STATE_DIR/pushed-domains" ;;
          esac
        done
        set +f
        IFS="$old_ifs"
        ;;
      SIG*" received, process restarting"|SIG*" received, process exiting")
        printf '%s\n' "$msg" | grep -Eq '^SIG[A-Z0-9]+\[[a-z0-9,-]*\] received, process (restarting|exiting)$' || continue
        set_state reconnecting "터널이 내려갔습니다"
        pids=$(pidof sockd 2>/dev/null) && kill $pids 2>/dev/null
        clear_dns
        rm -f "$STATE_DIR/pushed-dns" "$STATE_DIR/pushed-domains"
        ;;
      "Initialization Sequence Completed")
        if apply_dns; then
          gen=$(cat "$STATE_DIR/gen" 2>/dev/null || echo 0)
          echo $((gen + 1)) > "$STATE_DIR/gen"
          set_state up "터널 연결됨"
        else
          echo "[$NAME] 오류: /etc/resolv.conf 를 쓰지 못했습니다. 이름 해석이 터널 밖으로 샐 수 있어 SOCKS5 를 켜지 않습니다." >&2
          kill "$(cat "$STATE_DIR/openvpn.pid" 2>/dev/null)" 2>/dev/null
        fi
        ;;
    esac
  done
}
