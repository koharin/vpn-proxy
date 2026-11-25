#!/bin/sh
# OpenVPN 이 터널을 올린 뒤(재연결 포함) 부른다.
# VPN 이 알려 준 DNS 로 바꿔, SOCKS 로 들어온 이름(socks5h)을 VPN 안에서 해석한다.
# DNS 설정을 쓰지 못하면 터널을 '올라옴'으로 표시하지 않는다 → SOCKS5 가 켜지지 않는다.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
STATE_DIR=/run/vpn

NAME=$(cat "$STATE_DIR/name" 2>/dev/null || echo vpn)
servers=$(cat "$STATE_DIR/dns" 2>/dev/null || true)
domains=""
if [ -z "$servers" ]; then
  i=1
  while :; do
    eval "opt=\${foreign_option_$i:-}"
    [ -n "$opt" ] || break
    case "$opt" in
      "dhcp-option DNS "*) servers="$servers ${opt#dhcp-option DNS }" ;;
      "dhcp-option DOMAIN "*) domains="$domains ${opt#dhcp-option DOMAIN }" ;;
      "dhcp-option DOMAIN-SEARCH "*) domains="$domains ${opt#dhcp-option DOMAIN-SEARCH }" ;;
    esac
    i=$((i + 1))
  done
fi

content="# VPN DNS 없음: 이름 해석을 하지 않는다(VPN_DNS 로 지정)"
valid=""
for s in $(echo "$servers" | tr ',' ' '); do
  echo "$s" | grep -Eq '^[0-9A-Fa-f:.]+$' && valid="$valid $s"
done
if [ -n "$valid" ]; then
  content=""
  for s in $valid; do
    content="${content}nameserver $s
"
  done
  clean=""
  for d in $domains; do
    echo "$d" | grep -Eq '^[A-Za-z0-9._-]+$' && clean="$clean $d"
  done
  [ -n "$clean" ] && content="${content}search$clean
"
fi

# 쓰고 나서 다시 읽어 확인한다. 다르면(쓰기 실패) 실패로 끝낸다 — OpenVPN 도 함께 끝난다.
printf '%s' "$content" > /etc/resolv.conf 2>/dev/null
if [ "$(cat /etc/resolv.conf 2>/dev/null)" != "$(printf '%s' "$content")" ]; then
  echo "[$NAME] 오류: /etc/resolv.conf 를 쓰지 못했습니다. 이름 해석이 터널 밖으로 샐 수 있어 SOCKS5 를 켜지 않습니다." >&2
  exit 1
fi

if [ -n "$valid" ]; then
  echo "[$NAME] DNS: 터널 안의 DNS 를 씁니다 ($(echo $valid))"
else
  echo "[$NAME] 경고: VPN 이 DNS 를 알려 주지 않았습니다. 이름으로는 접속되지 않고 IP 주소로만 됩니다. 이름을 쓰려면 .env 의 VPN_x_DNS 에 내부 DNS 주소를 적으세요."
fi

gen=$(cat "$STATE_DIR/gen" 2>/dev/null || echo 0)
echo $((gen + 1)) > "$STATE_DIR/gen"
echo up > "$STATE_DIR/state"
exit 0
