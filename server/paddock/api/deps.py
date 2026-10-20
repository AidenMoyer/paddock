"""Request authentication and network policy."""

from __future__ import annotations

import ipaddress
import json
import time

from fastapi import Depends, HTTPException, Request

from .. import db
from ..actor import Actor, for_user
from ..config import settings
from ..security import token_hash

SESSION_COOKIE = "paddock_session"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


def network_zone(ip: str) -> str | None:
    """'loopback' | 'tunnel' | 'peer' | 'lan' | None (not allowed)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return "loopback" if settings.trust_loopback else None
    if addr in settings.wg_subnet:
        return "tunnel"
    from ..federation import is_peer_ip

    if is_peer_ip(str(addr)):
        return "peer"
    if any(addr in net for net in settings.lan_cidrs):
        return "lan"
    return None


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def _session_actor(token: str, request: Request) -> Actor | None:
    row = db.q1("SELECT s.*, u.username, u.is_admin, u.id AS uid FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=?", (token_hash(token),))
    if not row:
        return None
    now = time.time()
    if row["expires_at"] and row["expires_at"] < now:
        db.x("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
        return None
    if row["kind"] == "device":
        dev = db.q1("SELECT revoked_at FROM devices WHERE id=?", (row["device_id"],))
        if not dev or dev["revoked_at"]:
            return None
    if not row["last_seen"] or now - row["last_seen"] > 60:
        db.x("UPDATE sessions SET last_seen=?, ip=? WHERE token_hash=?", (now, client_ip(request), row["token_hash"]))
        if row["device_id"]:
            db.x("UPDATE devices SET last_seen=? WHERE id=?", (now, row["device_id"]))
    actor = for_user({"id": row["uid"], "username": row["username"], "is_admin": row["is_admin"]},
                     kind=row["kind"], device_id=row["device_id"])
    actor.remote = network_zone(client_ip(request)) == "tunnel"
    return actor


def actor_from_api_token(request: Request) -> Actor | None:
    tok = _bearer(request)
    if not tok or not tok.startswith("pdk_ai_"):
        return None
    row = db.q1("SELECT t.*, u.username, u.is_admin FROM api_tokens t JOIN users u ON u.id=t.user_id "
                "WHERE t.token_hash=? AND t.revoked_at IS NULL", (token_hash(tok),))
    if not row:
        return None
    scopes = set(json.loads(row["scopes"]))
    if not row["is_admin"]:
        scopes.discard("admin")
    now = time.time()
    if not row["last_used"] or now - row["last_used"] > 60:
        db.x("UPDATE api_tokens SET last_used=? WHERE id=?", (now, row["id"]))
    return Actor(user_id=row["user_id"], username=row["username"], is_admin=bool(row["is_admin"]), kind="token",
                 scopes=scopes, token_id=row["id"], label=f"agent:{row['name']}")


def optional_actor(request: Request) -> Actor | None:
    tok = _bearer(request)
    if tok:
        if tok.startswith("pdk_ai_"):
            return actor_from_api_token(request)
        return _session_actor(tok, request)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        actor = _session_actor(cookie, request)
        if actor and request.method not in ("GET", "HEAD", "OPTIONS"):
            # Cookie-authenticated writes must carry a header a cross-site form/page can't set.
            if request.headers.get("x-paddock") != "1":
                raise HTTPException(403, "Missing anti-forgery header")
        return actor
    return None


def current_actor(actor: Actor | None = Depends(optional_actor)) -> Actor:
    if actor is None:
        raise HTTPException(401, "Sign in required")
    return actor


def require_write(actor: Actor = Depends(current_actor)) -> Actor:
    if not actor.can("write"):
        raise HTTPException(403, "This credential is read-only")
    return actor


def require_admin(actor: Actor = Depends(current_actor)) -> Actor:
    if not actor.can("admin"):
        raise HTTPException(403, "Admins only")
    return actor


def require_human(actor: Actor = Depends(current_actor)) -> Actor:
    """Account/security operations are never available to API (agent) tokens."""
    if actor.kind == "token":
        raise HTTPException(403, "Not available to API tokens")
    return actor
