"""Linked servers: pair Paddock servers / Paddock Nodes the same way phones pair.

Trust model
-----------
* A server's identity is the SHA-256 of its private CA certificate (node_id).
* An invite is a one-time, 30-minute secret that carries the inviter's CA
  fingerprint. The joiner refuses to talk to anything whose CA doesn't match, and
  both admins compare a 6-word safety phrase derived from (fingerprint, secret) so
  a tampered invite is caught.
* Each side mints its own random token for the other to present; only hashes are
  stored by the issuer. Every peer call is HTTPS verified against the peer's own
  CA (pinned), over the LAN when both are home, otherwise over WireGuard.
* Remote pairing with nothing but the one WireGuard port: the invite includes a
  throwaway WireGuard key that the inviter accepts for 30 minutes on a reserved
  tunnel address. The joiner brings up a temporary interface with it, completes
  the handshake through it, and both sides then switch to their permanent keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import ssl
import tempfile
import threading
import time
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from . import __version__, db, tls, wireguard
from .config import settings
from .security import new_token, token_hash

log = logging.getLogger("paddock.federation")

INVITE_TTL = 30 * 60
INVITE_PREFIX = "pdk-link:"

_WORDS = """apple arrow atlas axle badge baker bamboo banjo barrel basin beacon bear bell bench berry bison blade blaze
block bloom board bolt boot bottle brick bridge brook brush bucket bugle cabin cable cactus camel candle canoe canyon
carbon cargo castle cedar chalk cherry chess chimney cider cinder circle citrus clay cliff clock cloud clover cobalt
comet compass copper coral cotton crane crater crayon creek cricket crown crystal dagger daisy delta desert diamond
dingo dolphin dragon drum dune eagle ember engine falcon feather fern ferry fiddle field flame flint flute forest
fossil fox galaxy garden garnet geyser ginger glacier globe goat gold granite grape gravel harbor harp hawk hazel
helmet heron hill honey horizon hornet iceberg igloo indigo iris island ivory jacket jade jaguar jasmine jelly jewel
kayak kettle kite koala ladder lagoon lantern larch lava lemon lens lilac lime linen lion lizard lobster lotus lunar
magnet mango maple marble meadow melon meteor mint mirror moose moss motor mountain nectar needle nickel nova oak
oasis ocean olive onyx orbit orchid otter owl paddle palm panda paper parrot peach pearl pebble pepper piano pilot
pine pixel planet plum pocket polar pony poppy prism pump quartz quill rabbit radar radio raven reef ribbon river
robin rocket rose ruby saddle sage salmon sand sapphire saturn scarf shell silver sketch sled slate socket solar
spark spider spruce squid star stone storm sugar summit sunset swan tango temple thistle thunder tiger timber toast
topaz torch tower trail tulip tundra turtle valley velvet violet walnut walrus wave whale willow wind wolf yarn
zebra zephyr zinc acorn anvil aurora basil birch""".split()
assert len(_WORDS) >= 256, len(_WORDS)


def role() -> str:
    import os

    return os.environ.get("PADDOCK_ROLE", "server")


# ---- identity -------------------------------------------------------------------------------

def ca_fingerprint(pem: bytes | str) -> str:
    if isinstance(pem, str):
        pem = pem.encode()
    crt = x509.load_pem_x509_certificate(pem)
    return hashlib.sha256(crt.public_bytes(serialization.Encoding.DER)).hexdigest()


def node_id() -> str:
    pem = tls.ca_pem()
    if not pem:
        tls.ensure_certificates()
        pem = tls.ca_pem()
    return ca_fingerprint(pem)


def safety_words(fingerprint: str, code: str) -> str:
    d = hashlib.sha256(f"paddock-link|{fingerprint}|{code}".encode()).digest()
    return " ".join(_WORDS[b] for b in d[:6])


def self_info() -> dict:
    from .api.auth import lan_urls, server_name

    return {"name": server_name(), "node_id": node_id(), "role": role(), "version": __version__,
            "lan": lan_urls(), "tunnel_ip": settings.wg_server_ip if settings.wg_enabled else None,
            "wg_pubkey": wireguard.server_keys()[1] if settings.wg_enabled else None,
            "wg_endpoint": wireguard.endpoint() if settings.wg_enabled else None, "api_port": settings.port}


# ---- invites ----------------------------------------------------------------------------------

def create_invite(user_id: int, grants: dict) -> dict:
    code = secrets.token_urlsafe(24)
    now = time.time()
    me = self_info()
    boot = None
    if settings.wg_enabled and me["wg_endpoint"]:
        priv, pub = wireguard.keypair()
        ip = wireguard.allocate_ip(high=True)
        boot = {"private_key": priv, "address": ip}
    db.x("INSERT INTO invites(code_hash, created_by, created_at, expires_at, grants, boot_pubkey, boot_ip) VALUES(?,?,?,?,?,?,?)",
         (token_hash(code), user_id, now, now + INVITE_TTL, json.dumps(_clean_grants(grants)),
          pub if boot else None, boot["address"] if boot else None))
    wireguard.sync()
    payload = {
        "v": 1, "name": me["name"], "node_id": me["node_id"], "role": me["role"], "code": code,
        "lan": me["lan"], "exp": now + INVITE_TTL, "grants": _clean_grants(grants),
        "tunnel": {"endpoint": me["wg_endpoint"], "server_pub": me["wg_pubkey"], "server_ip": me["tunnel_ip"],
                   "port": settings.port, **({"boot_private_key": boot["private_key"], "boot_ip": boot["address"]} if boot else {})}
        if boot else None,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"invite": INVITE_PREFIX + raw, "safety_words": safety_words(me["node_id"], code), "expires_at": payload["exp"]}


def _clean_grants(g: dict) -> dict:
    libs = [int(x) for x in (g.get("libraries") or []) if isinstance(x, int) or str(x).isdigit()]
    valid = {r["id"] for r in db.q("SELECT id FROM libraries")}
    return {"backup_quota_gb": max(0.0, float(g.get("backup_quota_gb") or 0)), "libraries": [l for l in libs if l in valid]}


def decode_invite(text: str) -> dict:
    text = text.strip()
    if not text.startswith(INVITE_PREFIX):
        raise ValueError("That isn't a Paddock invite")
    raw = text[len(INVITE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, json.JSONDecodeError):
        raise ValueError("The invite is damaged — copy it again")
    if data.get("v") != 1 or not data.get("code") or not data.get("node_id"):
        raise ValueError("Unsupported invite")
    if data["exp"] < time.time():
        raise ValueError("This invite has expired — ask for a new one")
    if data["node_id"] == node_id():
        raise ValueError("That invite is from this server")
    return data


def inspect_invite(text: str) -> dict:
    d = decode_invite(text)
    return {"name": d["name"], "role": d["role"], "grants": d.get("grants", {}),
            "safety_words": safety_words(d["node_id"], d["code"]), "remote_possible": bool(d.get("tunnel"))}


# ---- pinned HTTPS client ----------------------------------------------------------------------------

_ctx_cache: dict[str, ssl.SSLContext] = {}


def _ssl_for(ca_pem: str) -> ssl.SSLContext:
    key = hashlib.sha256(ca_pem.encode()).hexdigest()
    if key not in _ctx_cache:
        ctx = ssl.create_default_context(cadata=ca_pem)
        ctx.check_hostname = False  # addresses change (LAN/tunnel); the private CA is the pin
        _ctx_cache[key] = ctx
    return _ctx_cache[key]


def _fetch_ca(base: str, expect_fp: str, timeout: float = 4) -> str:
    """Download the peer's CA over a not-yet-trusted connection and accept it only if the fingerprint matches."""
    with httpx.Client(verify=False, timeout=timeout) as c:  # noqa: S501 - verified by fingerprint below
        r = c.get(f"{base}/api/ca.crt")
        r.raise_for_status()
    pem = r.text
    if ca_fingerprint(pem) != expect_fp:
        raise PermissionError("The server's identity doesn't match the invite — refusing to connect")
    return pem


class PeerClient:
    def __init__(self, peer: dict):
        self.peer = peer
        self.urls = json.loads(peer["urls"]) if isinstance(peer["urls"], str) else peer["urls"]

    def _client(self, timeout: float = 30) -> httpx.Client:
        verify = _ssl_for(self.peer["ca_pem"])
        return httpx.Client(verify=verify, timeout=timeout, headers={
            "Authorization": f"Bearer {self.peer['token_out']}", "X-Paddock-Node": node_id()})

    def request(self, method: str, path: str, timeout: float = 30, **kw) -> httpx.Response:
        last: Exception | None = None
        for base in self.urls:
            if base.startswith("http://") and not settings.dev_mode:
                continue
            try:
                with self._client(timeout) as c:
                    r = c.request(method, base + path, **kw)
                if r.status_code >= 500:
                    last = RuntimeError(f"{r.status_code} {r.text[:200]}")
                    continue
                db.x("UPDATE peers SET last_contact=?, last_error=NULL WHERE id=?", (time.time(), self.peer["id"]))
                # remember which address worked first next time
                if base != self.urls[0]:
                    self.urls.remove(base)
                    self.urls.insert(0, base)
                    db.x("UPDATE peers SET urls=? WHERE id=?", (json.dumps(self.urls), self.peer["id"]))
                return r
            except (httpx.HTTPError, ssl.SSLError) as e:
                last = e
        db.x("UPDATE peers SET last_error=? WHERE id=?", (str(last)[:300], self.peer["id"]))
        raise ConnectionError(f"{self.peer['name']} is unreachable ({type(last).__name__ if last else 'no address'})")

    def stream(self, method: str, path: str, **kw):
        """Context manager streaming a response (media proxying)."""
        for base in self.urls:
            if base.startswith("http://") and not settings.dev_mode:
                continue
            client = self._client(timeout=120)
            try:
                req = client.build_request(method, base + path, **kw)
                resp = client.send(req, stream=True)
                return client, resp
            except (httpx.HTTPError, ssl.SSLError):
                client.close()
                continue
        raise ConnectionError(f"{self.peer['name']} is unreachable")


def peer(pid: int) -> dict:
    p = db.q1("SELECT * FROM peers WHERE id=?", (pid,))
    if not p:
        raise KeyError("No such linked server")
    return p


def client(pid: int) -> PeerClient:
    return PeerClient(peer(pid))


# ---- handshake ---------------------------------------------------------------------------------------

def _tunnel_url(ip: str | None, port: int | None) -> list[str]:
    return [f"https://{ip}:{port or settings.port}"] if ip else []


def accept_invite(text: str) -> dict:
    """Run on the JOINING server: contact the inviter, exchange credentials, store the link."""
    inv = decode_invite(text)
    if db.q1("SELECT 1 FROM peers WHERE node_id=?", (inv["node_id"],)):
        raise ValueError(f"Already linked with {inv['name']}")
    me = self_info()
    my_token = new_token("pdk_peer")
    body = {"code": inv["code"], "name": me["name"], "node_id": me["node_id"], "role": me["role"],
            "ca_pem": tls.ca_pem().decode(), "urls": me["lan"], "wg_pubkey": me["wg_pubkey"],
            "tunnel_ip": me["tunnel_ip"], "wg_endpoint": me["wg_endpoint"], "api_port": me["api_port"],
            "token": my_token}

    attempts: list[str] = list(inv.get("lan") or [])
    boot_name = None
    errors = []
    ca_pem = None
    resp = None
    for base in attempts:
        try:
            ca_pem = _fetch_ca(base, inv["node_id"])
            with httpx.Client(verify=_ssl_for(ca_pem), timeout=20) as c:
                resp = c.post(f"{base}/api/peer/handshake", json=body)
            break
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001 - try the next path
            errors.append(f"{base}: {type(e).__name__}")
    if resp is None and inv.get("tunnel") and inv["tunnel"].get("boot_private_key"):
        if not settings.wg_enabled:
            raise ConnectionError("The other server isn't on this network and remote access (WireGuard) is off here")
        t = inv["tunnel"]
        boot_name = secrets.token_hex(3)
        wireguard.write_boot(boot_name, t["boot_private_key"], t["boot_ip"], t["server_pub"], t["endpoint"], t["server_ip"])
        base = f"https://{t['server_ip']}:{t['port']}"
        deadline = time.time() + 60
        try:
            while time.time() < deadline:  # wait for the sidecar to bring the temporary tunnel up
                try:
                    ca_pem = _fetch_ca(base, inv["node_id"], timeout=3)
                    with httpx.Client(verify=_ssl_for(ca_pem), timeout=20) as c:
                        resp = c.post(f"{base}/api/peer/handshake", json=body)
                    attempts.append(base)
                    break
                except PermissionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    errors.append(f"tunnel: {type(e).__name__}")
                    time.sleep(2)
        finally:
            wireguard.remove_boot(boot_name)
    if resp is None:
        raise ConnectionError("Couldn't reach the other server. " + "; ".join(errors[-3:]))
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:200]
        raise PermissionError(f"The other server refused: {detail}")
    them = resp.json()
    urls = [u for u in (them.get("urls") or []) if u.startswith("https://")] + _tunnel_url(them.get("tunnel_ip"), them.get("api_port"))
    now = time.time()
    pid = db.x("INSERT INTO peers(node_id, name, role, ca_pem, urls, wg_pubkey, tunnel_ip, wg_endpoint, token_out, token_in_hash, "
               "our_grants, their_grants, created_at, last_contact) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (inv["node_id"], them["name"], them.get("role", "server"), ca_pem, json.dumps(_dedupe(urls)),
                them.get("wg_pubkey"), them.get("tunnel_ip"), them.get("wg_endpoint"), them["token"], token_hash(my_token),
                json.dumps({"backup_quota_gb": 0, "libraries": []}), json.dumps(them.get("grants") or {}), now, now)).lastrowid
    wireguard.sync()
    db.audit("system", "federation.linked", peer=pid, name=them["name"], direction="joined")
    return {"id": pid, "name": them["name"]}


def _dedupe(xs: list[str]) -> list[str]:
    out: list[str] = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


def handle_handshake(body: dict, remote_ip: str) -> dict:
    """Run on the INVITING server when the joiner calls in."""
    now = time.time()
    code_h = token_hash(str(body.get("code", "")))
    with db.tx():
        inv = db.q1("SELECT * FROM invites WHERE code_hash=?", (code_h,))
        if not inv or inv["used_at"] or inv["expires_at"] < now:
            raise PermissionError("This invite is invalid or expired")
        if body.get("node_id") == node_id():
            raise PermissionError("Can't link a server to itself")
        ca_pem = body.get("ca_pem", "")
        if ca_fingerprint(ca_pem) != body.get("node_id"):
            raise PermissionError("Identity mismatch")
        if db.q1("SELECT 1 FROM peers WHERE node_id=?", (body["node_id"],)):
            raise PermissionError("Already linked")
        if body.get("wg_pubkey") and not wireguard.valid_public_key(body["wg_pubkey"]):
            raise PermissionError("Bad tunnel key")
        db.x("UPDATE invites SET used_at=? WHERE code_hash=?", (now, code_h))
        my_token = new_token("pdk_peer")
        urls = [u for u in (body.get("urls") or []) if isinstance(u, str) and u.startswith("https://")]
        urls += _tunnel_url(body.get("tunnel_ip"), body.get("api_port"))
        grants = json.loads(inv["grants"])
        pid = db.x("INSERT INTO peers(node_id, name, role, ca_pem, urls, wg_pubkey, tunnel_ip, wg_endpoint, token_out, token_in_hash, "
                   "our_grants, their_grants, created_at, last_contact) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (body["node_id"], str(body.get("name", "Paddock"))[:80], body.get("role", "server"), ca_pem,
                    json.dumps(_dedupe(urls)), body.get("wg_pubkey"), body.get("tunnel_ip"), body.get("wg_endpoint"),
                    body["token"], token_hash(my_token), json.dumps(grants), json.dumps({"backup_quota_gb": 0, "libraries": []}),
                    now, now)).lastrowid
    wireguard.sync()  # temporary invite key removed, permanent peer added
    refresh_peer_ips()
    db.audit("system", "federation.linked", peer=pid, name=body.get("name"), direction="invited", ip=remote_ip)
    me = self_info()
    return {"name": me["name"], "node_id": me["node_id"], "role": me["role"], "token": my_token, "urls": me["lan"],
            "wg_pubkey": me["wg_pubkey"], "tunnel_ip": me["tunnel_ip"], "wg_endpoint": me["wg_endpoint"],
            "api_port": me["api_port"], "grants": grants}


def authenticate(token: str) -> dict | None:
    if not token or not token.startswith("pdk_peer_"):
        return None
    return db.q1("SELECT * FROM peers WHERE token_in_hash=?", (token_hash(token),))


def unlink(pid: int, notify: bool = True) -> None:
    p = peer(pid)
    if notify:
        try:
            PeerClient(p).request("POST", "/api/peer/unlink", timeout=8)
        except Exception:  # noqa: BLE001 - unlink locally regardless
            pass
    db.x("DELETE FROM peers WHERE id=?", (pid,))
    wireguard.sync()
    refresh_peer_ips()
    db.audit("system", "federation.unlinked", peer=pid, name=p["name"])


def push_grants(pid: int) -> None:
    p = peer(pid)
    PeerClient(p).request("POST", "/api/peer/grants", json={"grants": json.loads(p["our_grants"])}, timeout=10)


# ---- liveness & endpoint exchange ------------------------------------------------------------------

_peer_ips: set[str] = set()


def refresh_peer_ips() -> None:
    global _peer_ips
    _peer_ips = {r["tunnel_ip"] for r in db.q("SELECT tunnel_ip FROM peers WHERE tunnel_ip IS NOT NULL")}


def is_peer_ip(ip: str) -> bool:
    return ip in _peer_ips


def hello(pid: int) -> dict:
    """Swap current addresses (public IP may have changed) and grants."""
    p = peer(pid)
    me = self_info()
    t0 = time.time()
    r = PeerClient(p).request("POST", "/api/peer/hello", json={"urls": me["lan"], "wg_endpoint": me["wg_endpoint"],
                                                               "name": me["name"], "grants": json.loads(p["our_grants"])}, timeout=10)
    r.raise_for_status()
    data = r.json()
    _apply_hello(pid, data)
    return {"ok": True, "ms": int((time.time() - t0) * 1000), "via": json.loads(peer(pid)["urls"])[0]}


def _apply_hello(pid: int, data: dict) -> None:
    p = peer(pid)
    urls = json.loads(p["urls"])
    tunnel = [u for u in urls if p["tunnel_ip"] and p["tunnel_ip"] in u]
    lan = [u for u in (data.get("urls") or []) if isinstance(u, str) and u.startswith("https://")]
    new_urls = _dedupe([u for u in urls if u not in tunnel] + lan + tunnel)
    changed_ep = data.get("wg_endpoint") and data.get("wg_endpoint") != p["wg_endpoint"]
    db.x("UPDATE peers SET urls=?, wg_endpoint=COALESCE(?, wg_endpoint), name=COALESCE(?, name), their_grants=COALESCE(?, their_grants), "
         "last_contact=? WHERE id=?", (json.dumps(new_urls), data.get("wg_endpoint"), (data.get("name") or None),
                                       json.dumps(data["grants"]) if isinstance(data.get("grants"), dict) else None, time.time(), pid))
    if changed_ep:
        wireguard.sync()


def handle_hello(p: dict, data: dict) -> dict:
    _apply_hello(p["id"], data)
    me = self_info()
    return {"urls": me["lan"], "wg_endpoint": me["wg_endpoint"], "name": me["name"],
            "grants": json.loads(peer(p["id"])["our_grants"])}


class Heartbeat(threading.Thread):
    def __init__(self, interval: int = 600):
        super().__init__(daemon=True, name="federation-heartbeat")
        self.interval = interval
        self.stop = threading.Event()

    def run(self) -> None:
        refresh_peer_ips()
        while not self.stop.wait(self.interval):
            for p in db.q("SELECT id FROM peers"):
                try:
                    hello(p["id"])
                except Exception as e:  # noqa: BLE001
                    log.info("peer %s unreachable: %s", p["id"], e)
