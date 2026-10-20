"""WireGuard configuration for off-network access and server-to-server links.

Paddock never runs the tunnel itself (that needs NET_ADMIN). It owns the *config*:
it writes data/wireguard/wg0.conf (plus short-lived boot-*.conf files used while
linking to a remote server), and the tiny `paddock-wg` sidecar (deploy/wireguard)
applies them with `wg syncconf`, maintains routes, enforces the firewall that
confines tunnel traffic to Paddock, and reports status back in status.json.

Security properties:
  * Phones and servers generate their own key pairs; only public keys travel.
  * Every peer's AllowedIPs is a single /32. A phone's tunnel only routes the
    server's tunnel address, and the sidecar's firewall drops anything arriving
    on wg0 that isn't for Paddock's HTTPS port — no access to the rest of the LAN
    or the NAS itself.
  * WireGuard doesn't answer unauthenticated packets, so the single forwarded
    UDP port is invisible to internet scanners.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import random
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from . import db
from .config import settings

_lock = threading.Lock()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def init_subnet() -> None:
    """Each install gets its own random /24 so linked servers never collide (unless pinned by env)."""
    if os.environ.get("PADDOCK_WG_SUBNET"):
        return
    p = settings.wg_dir / "subnet"
    settings.wg_dir.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        rng = random.SystemRandom()
        p.write_text(f"10.{rng.randint(64, 250)}.{rng.randint(1, 254)}.0/24")
    settings.wg_subnet = ipaddress.IPv4Network(p.read_text().strip())


def valid_public_key(key: str) -> bool:
    try:
        raw = base64.b64decode(key, validate=True)
        if len(raw) != 32:
            return False
        X25519PublicKey.from_public_bytes(raw)
        return True
    except Exception:
        return False


def keypair() -> tuple[str, str]:
    k = X25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(priv), _b64(pub)


def server_keys() -> tuple[str, str]:
    priv_path = settings.wg_dir / "server.key"
    settings.wg_dir.mkdir(parents=True, exist_ok=True)
    if not priv_path.exists():
        priv, _ = keypair()
        priv_path.write_text(priv)
        try:
            priv_path.chmod(0o600)
        except OSError:
            pass
    priv_b64 = priv_path.read_text().strip()
    k = X25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv_b64, _b64(pub)


def allocate_ip(high: bool = False) -> str:
    used = {r["wg_ip"] for r in db.q("SELECT wg_ip FROM devices WHERE wg_ip IS NOT NULL")}
    used |= {r["boot_ip"] for r in db.q("SELECT boot_ip FROM invites WHERE boot_ip IS NOT NULL")}
    hosts = list(settings.wg_subnet.hosts())[1:]  # .1 is the server
    if high:
        hosts.reverse()
    for h in hosts:
        if str(h) not in used:
            return str(h)
    raise RuntimeError("WireGuard subnet is full")


def render_config() -> str:
    priv, _ = server_keys()
    lines = [
        "# Managed by Paddock — edits are overwritten.",
        "[Interface]",
        f"PrivateKey = {priv}",
        f"Address = {settings.wg_server_ip}/{settings.wg_subnet.prefixlen}",
        f"ListenPort = {settings.wg_port}",
        "",
    ]
    for p in db.q("SELECT id, name, wg_pubkey, wg_ip FROM devices WHERE revoked_at IS NULL AND wg_pubkey IS NOT NULL ORDER BY id"):
        lines += [f"# device {p['id']}: {p['name'].replace(chr(10), ' ')}", "[Peer]", f"PublicKey = {p['wg_pubkey']}",
                  f"AllowedIPs = {p['wg_ip']}/32", ""]
    for s in db.q("SELECT id, name, wg_pubkey, tunnel_ip, wg_endpoint FROM peers WHERE wg_pubkey IS NOT NULL AND tunnel_ip IS NOT NULL"):
        lines += [f"# linked server {s['id']}: {s['name'].replace(chr(10), ' ')}", "[Peer]", f"PublicKey = {s['wg_pubkey']}",
                  f"AllowedIPs = {s['tunnel_ip']}/32"]
        if s["wg_endpoint"]:
            lines.append(f"Endpoint = {s['wg_endpoint']}")
        lines += ["PersistentKeepalive = 25", ""]
    now = time.time()
    for inv in db.q("SELECT boot_pubkey, boot_ip FROM invites WHERE boot_pubkey IS NOT NULL AND used_at IS NULL AND expires_at>?", (now,)):
        lines += ["# pending server invite (temporary)", "[Peer]", f"PublicKey = {inv['boot_pubkey']}", f"AllowedIPs = {inv['boot_ip']}/32", ""]
    return "\n".join(lines)


def _atomic_write(path, text: str) -> None:
    # 0640 + group 568: the capability-less sidecar (uid 0, gid 568) can read it; nobody else can.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    try:
        tmp.chmod(0o640)
    except OSError:
        pass
    os.replace(tmp, path)


def _prepare_dir() -> None:
    settings.wg_dir.mkdir(parents=True, exist_ok=True)
    try:
        settings.wg_dir.chmod(0o770)  # sidecar writes status.json via the shared group
    except OSError:
        pass


def sync() -> None:
    """Rewrite wg0.conf; the sidecar notices the change and applies it within a few seconds."""
    if not settings.wg_enabled:
        return
    with _lock:
        _prepare_dir()
        _atomic_write(settings.wg_dir / "wg0.conf", render_config())
        (settings.wg_dir / "allowed-port").write_text(str(settings.port))


def write_boot(name: str, private_key: str, address: str, peer_pub: str, endpoint: str, peer_ip: str) -> None:
    """Temporary interface used only while linking to a remote server for the first time."""
    _atomic_write(settings.wg_dir / f"boot-{name}.conf", "\n".join([
        "[Interface]", f"PrivateKey = {private_key}", f"Address = {address}/32", "",
        "[Peer]", f"PublicKey = {peer_pub}", f"Endpoint = {endpoint}", f"AllowedIPs = {peer_ip}/32",
        "PersistentKeepalive = 5", ""]))


def remove_boot(name: str) -> None:
    (settings.wg_dir / f"boot-{name}.conf").unlink(missing_ok=True)


def status() -> dict:
    """Live view from the sidecar (handshakes, transfer). Empty if the sidecar isn't running."""
    p = settings.wg_dir / "status.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"up": False, "peers": []}
    data["up"] = time.time() - data.get("updated", 0) < 60
    for peer in data.get("peers", []):
        peer["public_key_prefix"] = peer.get("public_key", "")[:8]
        peer.pop("public_key", None)
    return data


def endpoint() -> str | None:
    """host:port phones and linked servers should dial, without any outside service."""
    from . import portmap

    remote = db.get_setting("remote", {})
    if remote.get("endpoint_host"):
        host = remote["endpoint_host"].strip()
        return host if host.count(":") == 1 else f"{host}:{settings.wg_port}"
    if settings.wg_endpoint:  # set in the app config, may carry its own external port
        return settings.wg_endpoint if ":" in settings.wg_endpoint else f"{settings.wg_endpoint}:{settings.wg_port}"
    ip = portmap.public_ip()
    return f"{ip}:{settings.wg_port}" if ip else None


def client_profile(device_ip: str) -> dict:
    """What the phone needs to bring up its app-only tunnel (minus its private key)."""
    _, pub = server_keys()
    ep = endpoint()
    return {
        "enabled": settings.wg_enabled and bool(ep),
        "endpoint": ep,
        "server_public_key": pub,
        "address": f"{device_ip}/32",
        "allowed_ips": f"{settings.wg_server_ip}/32",
        "server_ip": settings.wg_server_ip,
        "api_port": settings.port,
        "keepalive": 25,
    }


def is_tunnel_address(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in settings.wg_subnet
    except ValueError:
        return False
