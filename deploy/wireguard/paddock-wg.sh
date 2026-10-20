#!/bin/sh
# paddock-wg — WireGuard for Paddock, applied from files Paddock writes into $WG_DIR.
#
#   wg0.conf        main interface: phones + linked servers (+ temporary invite keys)
#   boot-*.conf     short-lived client interfaces used while linking to a remote server
#   allowed-port    Paddock's HTTPS port (the only thing tunnel peers may reach)
#   status.json     written here: handshakes and traffic, for the admin UI
#
# Firewall: anything arriving on a Paddock interface may reach Paddock's port and nothing
# else on this host, and is never forwarded to the LAN. Rules live in their own chains and
# are removed on exit.
set -eu

WG_DIR="${WG_DIR:-/data/wireguard}"
IFACE=wg0
CHAIN=PADDOCK_WG
IPT=iptables
command -v iptables-nft >/dev/null 2>&1 && iptables-nft -L >/dev/null 2>&1 && IPT=iptables-nft

log() { echo "[paddock-wg] $*"; }

conf_hash() { [ -f "$1" ] && sha256sum "$1" | cut -d' ' -f1 || echo none; }

addr_of() { sed -n 's/^[[:space:]]*Address[[:space:]]*=[[:space:]]*//p' "$1" | head -n1 | tr -d ' '; }

allowed_ips() { sed -n 's/^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*//p' "$1" | tr ',' '\n' | tr -d ' ' | grep -v '^$' || true; }

# Strip wg-quick-only keys so `wg setconf/syncconf` accepts the file.
stripped() { sed -E '/^[[:space:]]*(Address|DNS|MTU|Table|PreUp|PostUp|PreDown|PostDown|SaveConfig)[[:space:]]*=/d' "$1"; }

firewall_up() {
  port="$(cat "$WG_DIR/allowed-port" 2>/dev/null || echo 8443)"
  $IPT -N $CHAIN 2>/dev/null || $IPT -F $CHAIN
  $IPT -A $CHAIN -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  $IPT -A $CHAIN -p tcp --dport "$port" -j ACCEPT
  $IPT -A $CHAIN -p icmp -j ACCEPT
  $IPT -A $CHAIN -j DROP
  for i in $IFACE "pdkb+"; do
    $IPT -C INPUT -i "$i" -j $CHAIN 2>/dev/null || $IPT -I INPUT -i "$i" -j $CHAIN
    $IPT -C FORWARD -i "$i" -j DROP 2>/dev/null || $IPT -I FORWARD -i "$i" -j DROP
    $IPT -C FORWARD -o "$i" -j DROP 2>/dev/null || $IPT -I FORWARD -o "$i" -j DROP
  done
  log "firewall: tunnel peers may reach tcp/$port only; no forwarding"
}

firewall_down() {
  for i in $IFACE "pdkb+"; do
    $IPT -D INPUT -i "$i" -j $CHAIN 2>/dev/null || true
    $IPT -D FORWARD -i "$i" -j DROP 2>/dev/null || true
    $IPT -D FORWARD -o "$i" -j DROP 2>/dev/null || true
  done
  $IPT -F $CHAIN 2>/dev/null || true
  $IPT -X $CHAIN 2>/dev/null || true
}

# Bring an interface up from a Paddock conf (idempotent), then sync peers and routes.
apply() {
  name="$1"; conf="$2"
  if ! ip link show "$name" >/dev/null 2>&1; then
    ip link add "$name" type wireguard
    ip address add "$(addr_of "$conf")" dev "$name"
    tmp="$(mktemp)"; stripped "$conf" > "$tmp"; wg setconf "$name" "$tmp"; rm -f "$tmp"
    ip link set "$name" up
    log "$name up ($(addr_of "$conf"))"
  else
    tmp="$(mktemp)"; stripped "$conf" > "$tmp"; wg syncconf "$name" "$tmp"; rm -f "$tmp"
  fi
  # routes for peers outside the interface's own subnet (linked servers, boot targets)
  want="$(allowed_ips "$conf" | sort -u)"
  for cidr in $want; do ip route replace "$cidr" dev "$name" 2>/dev/null || true; done
  ip route show dev "$name" proto boot 2>/dev/null | awk '{print $1}' | while read -r r; do
    echo "$want" | grep -qx "$r" || ip route del "$r" dev "$name" 2>/dev/null || true
  done
}

write_status() {
  wg show all dump 2>/dev/null | awk -F'\t' 'NF==9 {print $1"\t"$2"\t"$4"\t"$5"\t"$6"\t"$7"\t"$8}' | \
    jq -R -s --arg now "$(date +%s)" '
      {updated: ($now|tonumber),
       peers: [split("\n")[] | select(length>0) | split("\t") |
               {interface: .[0], public_key: .[1], endpoint: (if .[2]=="(none)" then null else .[2] end),
                allowed_ips: .[3], latest_handshake: (.[4]|tonumber), rx: (.[5]|tonumber), tx: (.[6]|tonumber)}]}' \
    > "$WG_DIR/status.json.tmp" && chmod 0644 "$WG_DIR/status.json.tmp" && mv -f "$WG_DIR/status.json.tmp" "$WG_DIR/status.json"
}

cleanup() {
  log "shutting down"
  for i in $(ip -o link show type wireguard 2>/dev/null | awk -F': ' '{print $2}'); do
    case "$i" in $IFACE|pdkb*) ip link del "$i" 2>/dev/null || true ;; esac
  done
  firewall_down
  exit 0
}
trap cleanup INT TERM

log "waiting for $WG_DIR/wg0.conf (enable remote access in Paddock)"
while [ ! -f "$WG_DIR/wg0.conf" ]; do sleep 3; done
firewall_up

last=""
while true; do
  h="$(conf_hash "$WG_DIR/wg0.conf")"
  if [ "$h" != "$last" ]; then
    apply $IFACE "$WG_DIR/wg0.conf" && last="$h" && log "$IFACE synced"
  fi
  # temporary linking interfaces
  for f in "$WG_DIR"/boot-*.conf; do
    [ -f "$f" ] || continue
    n="pdkb$(basename "$f" .conf | sed 's/^boot-//' | cut -c1-8)"
    apply "$n" "$f"
  done
  for i in $(ip -o link show type wireguard 2>/dev/null | awk -F': ' '{print $2}' | grep '^pdkb' || true); do
    [ -f "$WG_DIR/boot-${i#pdkb}.conf" ] || { ip link del "$i"; log "$i removed"; }
  done
  write_status || true
  sleep 3 &
  wait $!
done
