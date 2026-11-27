#!/bin/sh
[ "$(cat /run/vpn/state 2>/dev/null)" = "up" ] || exit 1
ip link show tun0 >/dev/null 2>&1 || exit 1
nc -z 127.0.0.1 "${SOCKS_PORT:-1080}" || exit 1
