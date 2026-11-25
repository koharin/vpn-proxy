#!/bin/sh
# OpenVPN 이 터널을 내릴 때(재연결 포함) 부른다. 표시를 내리면 entrypoint 가 SOCKS5 를 끈다.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
echo down > /run/vpn/state
# 2초 간격 확인을 기다리지 않고 SOCKS5 를 바로 끈다.
pids=$(pidof sockd 2>/dev/null) && kill $pids 2>/dev/null
# DNS 는 Docker DNS 로 되돌리지 않고 비운다(터널 밖으로 이름을 묻지 않게).
# VPN 서버 주소는 /etc/hosts 에 고정돼 있어 재연결에 DNS 가 필요 없다.
echo "# 터널이 내려감: 이름 해석을 하지 않는다" > /etc/resolv.conf 2>/dev/null
exit 0
