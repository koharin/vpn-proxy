#!/bin/sh
# ovpn 검사: 허용 목록에 있는 지시만 받는다. 문제가 있으면 줄 번호와 이유를 찍고 1 로 끝난다.
#   사용: check-ovpn.sh <ovpn 파일>
#
# 목적: ovpn 을 만든 쪽이 컨테이너 안에서 명령을 실행하거나, 라이브러리를 불러오거나,
# 파일을 쓰거나, 방화벽을 끄지 못하게 한다.
#
# 원칙
#   1. 글자: 인쇄 가능한 ASCII, 탭, 줄 끝의 CR 만 받는다. 세로 탭·폼 피드·줄 중간 CR·BOM·NUL·
#      한글 등 그 밖의 바이트가 있으면 멈춘다. OpenVPN 은 이런 글자를 공백처럼 건너뛰기도 해서,
#      검사와 OpenVPN 이 같은 줄을 다르게 읽는 틈이 생긴다. 255자를 넘는 줄도 멈춘다.
#   2. 지시: 줄의 첫 단어가 소문자·숫자·'-' 로만 되어 있고 아래 허용 목록에 있어야 한다.
#      목록에 없는 지시는 이유를 따지지 않고 받지 않는다.
#   3. 블록: <ca> 같은 인라인 블록은 허용 목록의 이름만 받는다. 닫는 표시는 OpenVPN 과 같은 기준으로
#      본다 — 앞 공백을 뺀 줄이 '</이름>' 으로 시작하면 닫는 것이다. 그런 줄이 정확히 '</이름>' 이
#      아니면(뒤에 다른 글자가 있으면) 멈춘다.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
file="${1:?ovpn 파일 경로가 필요합니다}"
rc=0

# ── 1. 글자 ────────────────────────────────────────────────────────────────
# 허용한 바이트를 모두 지우고 남는 것이 있으면 받지 않는 글자가 있는 것이다(NUL 포함).
left=$(tr -d '\011\012\015\040-\176' < "$file" | wc -c | tr -d ' ')
if [ "$left" != "0" ]; then
  echo "  받지 않는 글자가 ${left}바이트 있습니다(인쇄 가능한 ASCII·탭만 받습니다. 세로 탭, 폼 피드, BOM, 한글 주석 등은 지우세요)"
  awk '{ l = $0; sub(/\r$/, "", l); if (l ~ /[^\t -~]/) printf "  %d번째 줄: 받지 않는 글자\n", NR }' "$file" | head -20
  rc=1
fi
# NUL 은 도구마다 줄을 다르게 자르므로 따로 센다.
nul=$(tr -cd '\000' < "$file" | wc -c | tr -d ' ')
[ "$nul" = "0" ] || { echo "  NUL 문자가 ${nul}개 있습니다"; rc=1; }
[ "$rc" = "0" ] || exit 1

# ── 2·3. 지시와 블록 ───────────────────────────────────────────────────────
awk '
  BEGIN {
    # 클라이언트 접속에 쓰는 지시만. 명령 실행·라이브러리·파일 쓰기·원격 제어 지시는 넣지 않는다.
    n = split( \
      "client pull tls-client dev dev-type proto remote remote-random remote-random-hostname resolv-retry " \
      "nobind port lport rport float persist-key persist-tun persist-local-ip persist-remote-ip " \
      "connect-retry connect-retry-max connect-timeout server-poll-timeout explicit-exit-notify " \
      "keepalive ping ping-restart ping-exit ping-timer-rem inactive " \
      "reneg-sec reneg-bytes reneg-pkts hand-window tran-window tls-timeout tls-exit single-session " \
      "cipher data-ciphers data-ciphers-fallback ncp-ciphers ncp-disable auth auth-nocache auth-retry auth-user-pass " \
      "tls-version-min tls-version-max tls-cipher tls-ciphersuites tls-groups tls-cert-profile " \
      "tls-auth tls-crypt tls-crypt-v2 key-direction " \
      "remote-cert-tls remote-cert-ku remote-cert-eku verify-x509-name ns-cert-type peer-fingerprint " \
      "ca cert key extra-certs pkcs12 dh crl-verify " \
      "comp-lzo compress allow-compression comp-noadapt " \
      "mssfix fragment tun-mtu tun-mtu-extra link-mtu mtu-disc mtu-test sndbuf rcvbuf txqueuelen fast-io " \
      "mute mute-replay-warnings verb replay-window disable-occ " \
      "route route-metric route-delay route-nopull route-gateway redirect-gateway redirect-private " \
      "allow-pull-fqdn pull-filter ifconfig ifconfig-nowarn topology dhcp-option block-outside-dns " \
      "ignore-unknown-option push-peer-info user group", a, " ")
    for (i = 1; i <= n; i++) allowed[a[i]] = 1
    # 내용을 지시로 읽지 않는 블록(인증서·키). connection 블록은 안쪽 줄을 지시로 검사한다.
    n = split("ca cert key extra-certs dh pkcs12 secret tls-auth tls-crypt tls-crypt-v2 crl-verify " \
              "auth-user-pass peer-fingerprint", b, " ")
    for (i = 1; i <= n; i++) inline_ok[b[i]] = 1
    n = split("CLIENT_CERT FRIENDLY_NAME GENERIC_CONFIG FORWARD_COMPATIBLE PUSH_PEER_INFO", c, " ")
    for (i = 1; i <= n; i++) env_ok[c[i]] = 1
    skip = ""; skip_from = 0; in_conn = 0; rc = 0
  }
  function report(why) { printf "  %d번째 줄: %s\n", NR, why; rc = 1 }
  function ltrim(s) { sub(/^[ \t]+/, "", s); return s }
  function rtrim(s) { sub(/[ \t]+$/, "", s); return s }

  # 지시 한 줄(앞 공백을 뺀 것)을 검사한다. depth 는 "setenv opt" 가 겹친 횟수.
  function check(line, depth,    word, rest, name) {
    word = line; sub(/[ \t].*$/, "", word)
    rest = ltrim(substr(line, length(word) + 1))
    if (word !~ /^[a-z0-9-]+$/) { report("지시 이름이 소문자·숫자·\"-\" 가 아닙니다"); return }
    if (word == "setenv") {
      name = rest; sub(/[ \t].*$/, "", name)
      # "setenv opt <지시>" 는 OpenVPN 이 그 지시를 그대로 실행한다. 뒤의 지시도 같은 기준으로 검사한다.
      if (name == "opt") {
        if (depth >= 2) { report("setenv opt 가 너무 많이 겹쳤습니다"); return }
        rest = ltrim(substr(rest, 4))
        if (rest == "") { report("setenv opt 뒤에 지시가 없습니다"); return }
        check(rest, depth + 1)
        return
      }
      if (!(name in env_ok)) report("받지 않는 지시 \"setenv " name "\"")
      return
    }
    if (!(word in allowed)) { report("받지 않는 지시 \"" word "\""); return }
    # DNS 를 컨테이너 자신(127.x)이나 0.x 로 돌리면 터널 밖(Docker DNS)으로 이름을 묻게 된다.
    if (word == "dhcp-option" && rest ~ /^DNS[ \t]+(127\.|0\.|::|localhost)/) report("dhcp-option DNS 에 이 주소는 쓸 수 없습니다")
  }
  {
    raw = $0
    sub(/\r$/, "", raw)
    if (raw ~ /\r/) { report("줄 중간에 CR 이 있습니다"); next }
    if (length(raw) > 255) { report("줄이 255자를 넘습니다"); next }
    line = ltrim(raw)

    if (skip != "") {
      # OpenVPN 기준: 앞 공백을 뺀 줄이 닫는 표시로 "시작"하면 블록이 끝난다.
      close_tag = "</" skip ">"
      if (substr(line, 1, length(close_tag)) == close_tag) {
        if (rtrim(line) != close_tag) report("블록 닫는 표시 " close_tag " 뒤에 다른 글자가 있습니다")
        skip = ""
      }
      next
    }
    if (line == "" || line ~ /^[#;]/) next

    if (line ~ /^</) {
      tag = rtrim(line)
      if (tag == "<connection>") { if (in_conn) report("<connection> 이 겹쳤습니다"); in_conn = 1; next }
      if (tag == "</connection>") { if (!in_conn) report("짝이 없는 </connection>"); in_conn = 0; next }
      if (tag !~ /^<[a-z0-9-]+>$/) { report("블록 표시 형식이 다릅니다(<이름> 만, 뒤에 다른 글자 없이)"); next }
      name = substr(tag, 2, length(tag) - 2)
      if (!(name in inline_ok)) { report("받지 않는 블록 <" name ">"); next }
      skip = name; skip_from = NR
      next
    }
    check(line, 0)
  }
  END {
    if (skip != "") { printf "  %d번째 줄: 블록 <%s> 가 닫히지 않았습니다\n", skip_from, skip; rc = 1 }
    if (in_conn) { printf "  <connection> 이 닫히지 않았습니다\n"; rc = 1 }
    exit rc
  }' "$file" || rc=1
exit "$rc"
