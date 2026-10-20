# paddock-wg: applies the WireGuard config Paddock writes, and fences the tunnel in.
# Tiny on purpose: wireguard-tools, iproute2, nftables-backed iptables, jq. No listening services.
FROM alpine:3.22
RUN apk add --no-cache wireguard-tools-wg wireguard-tools-wg-quick iproute2 iptables iptables-legacy jq coreutils
COPY paddock-wg.sh /usr/local/bin/paddock-wg.sh
RUN chmod 0755 /usr/local/bin/paddock-wg.sh
ENTRYPOINT ["/usr/local/bin/paddock-wg.sh"]
