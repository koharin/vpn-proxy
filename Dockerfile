# VPN 하나 = 컨테이너 하나: OpenVPN 클라이언트 + SOCKS5 서버(dante).
# 다시 빌드해도 같은 베이스가 쓰이도록 정확한 버전과 다이제스트로 고정한다.
# 올릴 때: docker buildx imagetools inspect alpine:<새 버전> --format '{{.Manifest.Digest}}'
FROM alpine:3.22.8@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f

RUN apk add --no-cache openvpn dante-server iptables bind-tools tini

COPY rootfs/entrypoint.sh rootfs/vpn-up.sh rootfs/vpn-down.sh rootfs/healthcheck.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/vpn-up.sh \
      /usr/local/bin/vpn-down.sh /usr/local/bin/healthcheck.sh

# 터널이 올라와 있고 SOCKS 포트가 열려 있을 때만 healthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=2 \
  CMD /usr/local/bin/healthcheck.sh

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
