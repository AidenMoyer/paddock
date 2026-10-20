"""Accounts, sessions, device pairing, and API tokens for AI agents."""

from __future__ import annotations

import json
import secrets
import time

import segno
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import __version__, db, tls, wireguard
from ..actor import Actor
from ..config import settings
from ..security import (dummy_verify, hash_password, login_throttle, new_token, pairing_throttle, password_problems,
                        token_hash, verify_password)
from .deps import (SESSION_COOKIE, client_ip, current_actor, network_zone, optional_actor, require_admin,
                   require_human)

router = APIRouter(prefix="/api")

PAIRING_TTL = 10 * 60


def server_name() -> str:
    return db.get_setting("server_name", "Paddock")


def _user_public(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u.get("display_name") or u["username"],
            "is_admin": bool(u["is_admin"]), "allow_ai_changes": bool(u.get("allow_ai_changes"))}


def _start_web_session(response: Response, request: Request, user_id: int) -> None:
    tok = new_token("pdk_web")
    now = time.time()
    db.x("INSERT INTO sessions(token_hash, user_id, kind, created_at, expires_at, last_seen, ip) VALUES(?,?,?,?,?,?,?)",
         (token_hash(tok), user_id, "web", now, now + settings.session_days * 86400, now, client_ip(request)))
    response.set_cookie(SESSION_COOKIE, tok, max_age=settings.session_days * 86400, httponly=True,
                        secure=settings.tls, samesite="strict", path="/")


# ---- state / setup / login ---------------------------------------------------------

@router.get("/auth/state")
def auth_state(request: Request, actor: Actor | None = Depends(optional_actor)):
    user = db.q1("SELECT * FROM users WHERE id=?", (actor.user_id,)) if actor else None
    return {
        "setup_required": db.q1("SELECT COUNT(*) c FROM users")["c"] == 0,
        "user": _user_public(user) if user else None,
        "server": {"name": server_name(), "version": __version__},
        "zone": network_zone(client_ip(request)),
    }


class SetupBody(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(max_length=200)
    server_name: str = Field(default="Paddock", max_length=60)


@router.post("/auth/setup")
def setup(body: SetupBody, request: Request, response: Response):
    if network_zone(client_ip(request)) not in ("lan", "loopback"):
        raise HTTPException(403, "First-time setup must be done from your home network")
    with db.tx():
        if db.q1("SELECT COUNT(*) c FROM users")["c"] > 0:
            raise HTTPException(409, "Already set up")
        if problem := password_problems(body.password):
            raise HTTPException(400, problem)
        uid = db.x("INSERT INTO users(username, display_name, pw_hash, is_admin, created_at) VALUES(?,?,?,1,?)",
                   (body.username.strip(), body.username.strip(), hash_password(body.password), time.time())).lastrowid
        db.set_setting("server_name", body.server_name.strip() or "Paddock")
    db.audit(f"web:{body.username}", "setup.complete")
    _start_web_session(response, request, uid)
    return {"ok": True}


class LoginBody(BaseModel):
    username: str = Field(max_length=40)
    password: str = Field(max_length=200)


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = client_ip(request)
    keys = (f"ip:{ip}", f"user:{body.username.lower()}")
    wait = login_throttle.retry_after(*keys)
    if wait > 0:
        raise HTTPException(429, f"Too many attempts. Try again in {int(wait) + 1} seconds.",
                            headers={"Retry-After": str(int(wait) + 1)})
    user = db.q1("SELECT * FROM users WHERE username=?", (body.username.strip(),))
    if not user:
        dummy_verify(body.password)
    if not user or not verify_password(body.password, user["pw_hash"]):
        login_throttle.fail(*keys)
        db.audit(f"ip:{ip}", "login.failed", username=body.username[:40])
        raise HTTPException(401, "Wrong username or password")
    login_throttle.reset(*keys)
    _start_web_session(response, request, user["id"])
    db.audit(f"web:{user['username']}", "login", ip=ip)
    return {"user": _user_public(user)}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        db.x("DELETE FROM sessions WHERE token_hash=?", (token_hash(tok),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class PasswordBody(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


@router.post("/auth/password")
def change_password(body: PasswordBody, request: Request, actor: Actor = Depends(require_human)):
    user = db.q1("SELECT * FROM users WHERE id=?", (actor.user_id,))
    if not verify_password(body.current, user["pw_hash"]):
        raise HTTPException(401, "Current password is wrong")
    if problem := password_problems(body.new):
        raise HTTPException(400, problem)
    db.x("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(body.new), actor.user_id))
    # sign out every other web session for this user
    current = request.cookies.get(SESSION_COOKIE)
    db.x("DELETE FROM sessions WHERE user_id=? AND kind='web' AND token_hash!=?",
         (actor.user_id, token_hash(current) if current else ""))
    db.audit(actor.audit_name, "password.changed")
    return {"ok": True}


class PrefsBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=60)
    allow_ai_changes: bool | None = None


@router.patch("/me")
def update_me(body: PrefsBody, actor: Actor = Depends(require_human)):
    if body.display_name is not None:
        db.x("UPDATE users SET display_name=? WHERE id=?", (body.display_name.strip(), actor.user_id))
    if body.allow_ai_changes is not None:
        db.x("UPDATE users SET allow_ai_changes=? WHERE id=?", (1 if body.allow_ai_changes else 0, actor.user_id))
        db.audit(actor.audit_name, "assistant.write_access", enabled=body.allow_ai_changes)
    return _user_public(db.q1("SELECT * FROM users WHERE id=?", (actor.user_id,)))


# ---- users (admin) -----------------------------------------------------------------------

class NewUser(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(max_length=200)
    is_admin: bool = False


@router.get("/users")
def list_users(actor: Actor = Depends(require_admin)):
    return [_user_public(u) | {"created_at": u["created_at"]} for u in db.q("SELECT * FROM users ORDER BY id")]


@router.post("/users")
def create_user(body: NewUser, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    if problem := password_problems(body.password):
        raise HTTPException(400, problem)
    if db.q1("SELECT 1 FROM users WHERE username=?", (body.username.strip(),)):
        raise HTTPException(409, "That username is taken")
    uid = db.x("INSERT INTO users(username, display_name, pw_hash, is_admin, created_at) VALUES(?,?,?,?,?)",
               (body.username.strip(), body.username.strip(), hash_password(body.password), int(body.is_admin),
                time.time())).lastrowid
    db.audit(actor.audit_name, "user.created", user=body.username, admin=body.is_admin)
    return {"id": uid}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    if user_id == actor.user_id:
        raise HTTPException(400, "You can't delete yourself")
    db.x("DELETE FROM users WHERE id=?", (user_id,))
    wireguard.sync()
    db.audit(actor.audit_name, "user.deleted", user_id=user_id)
    return {"ok": True}


# ---- device pairing -------------------------------------------------------------------------

def lan_urls() -> list[str]:
    configured = db.get_setting("lan_urls")
    if configured:
        return configured
    scheme = "https" if settings.tls else "http"
    out = []
    for h in settings.hostnames:
        if h not in ("localhost",):
            out.append(f"{scheme}://{h}:{settings.port}")
    for ip in tls._local_ips():
        if not ip.startswith("127.") and ":" not in ip and not wireguard.is_tunnel_address(ip):
            out.append(f"{scheme}://{ip}:{settings.port}")
    return list(dict.fromkeys(out))


@router.post("/devices/pairing")
def start_pairing(request: Request, actor: Actor = Depends(require_human)):
    """Create a one-time code shown as a QR code. Valid 10 minutes, single use."""
    code = secrets.token_urlsafe(24)
    now = time.time()
    db.x("DELETE FROM pairing_codes WHERE expires_at<?", (now,))
    db.x("INSERT INTO pairing_codes(code_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
         (token_hash(code), actor.user_id, now, now + PAIRING_TTL))
    payload = {
        "v": 1,
        "app": "paddock",
        "name": server_name(),
        "lan": lan_urls(),
        "pin": tls.spki_pin(),
        "fp": tls.server_fingerprint(),
        "code": code,
        "wg": bool(settings.wg_enabled and settings.wg_endpoint),
    }
    text = json.dumps(payload, separators=(",", ":"))
    qr = segno.make(text, error="m")
    svg = qr.svg_inline(scale=5, dark="#0f0f0f", light="#ffffff", border=4, omitsize=True)
    db.audit(actor.audit_name, "pairing.started")
    return {"payload": payload, "qr_svg": svg, "expires_at": now + PAIRING_TTL}


class PairBody(BaseModel):
    code: str = Field(max_length=100)
    name: str = Field(min_length=1, max_length=60)
    platform: str = Field(default="android", max_length=20)
    wg_public_key: str | None = Field(default=None, max_length=64)


@router.post("/pair")
def complete_pairing(body: PairBody, request: Request):
    """Called by the phone app with the scanned code. Returns the device's long-lived token."""
    ip = client_ip(request)
    zone = network_zone(ip)
    if zone not in ("lan", "loopback") and not settings.allow_remote_pairing:
        raise HTTPException(403, "Pair new devices while connected to your home Wi-Fi")
    wait = pairing_throttle.retry_after(f"ip:{ip}")
    if wait > 0:
        raise HTTPException(429, "Too many attempts", headers={"Retry-After": str(int(wait) + 1)})
    if body.wg_public_key and not wireguard.valid_public_key(body.wg_public_key):
        raise HTTPException(400, "Invalid WireGuard public key")
    now = time.time()
    with db.tx():
        row = db.q1("SELECT * FROM pairing_codes WHERE code_hash=?", (token_hash(body.code),))
        if not row or row["used_at"] or row["expires_at"] < now:
            pairing_throttle.fail(f"ip:{ip}")
            raise HTTPException(401, "This pairing code is invalid or expired — make a new one")
        db.x("UPDATE pairing_codes SET used_at=? WHERE code_hash=?", (now, row["code_hash"]))
        wg_ip = wireguard.allocate_ip() if body.wg_public_key else None
        dev_id = db.x("INSERT INTO devices(user_id, name, platform, wg_pubkey, wg_ip, created_at, last_seen) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (row["user_id"], body.name.strip(), body.platform, body.wg_public_key, wg_ip, now, now)).lastrowid
        tok = new_token("pdk_dev")
        db.x("INSERT INTO sessions(token_hash, user_id, kind, device_id, created_at, last_seen, ip) "
             "VALUES(?,?,?,?,?,?,?)", (token_hash(tok), row["user_id"], "device", dev_id, now, now, ip))
    wireguard.sync()
    user = db.q1("SELECT * FROM users WHERE id=?", (row["user_id"],))
    db.audit(f"device:{body.name}", "device.paired", device_id=dev_id, user=user["username"], ip=ip)
    return {
        "token": tok,
        "device_id": dev_id,
        "user": _user_public(user),
        "server": {"name": server_name(), "version": __version__, "lan": lan_urls()},
        "wireguard": wireguard.client_profile(wg_ip) if wg_ip else {"enabled": False},
    }


@router.get("/devices")
def list_devices(actor: Actor = Depends(current_actor)):
    sql = ("SELECT d.id, d.name, d.platform, d.wg_ip, d.created_at, d.last_seen, d.revoked_at, u.username "
           "FROM devices d JOIN users u ON u.id=d.user_id")
    if actor.is_admin:
        return db.q(sql + " ORDER BY d.id DESC")
    return db.q(sql + " WHERE d.user_id=? ORDER BY d.id DESC", (actor.user_id,))


@router.delete("/devices/{device_id}")
def revoke_device(device_id: int, actor: Actor = Depends(require_human)):
    dev = db.q1("SELECT * FROM devices WHERE id=?", (device_id,))
    if not dev or (dev["user_id"] != actor.user_id and not actor.is_admin):
        raise HTTPException(404, "No such device")
    with db.tx():
        db.x("UPDATE devices SET revoked_at=?, wg_pubkey=NULL WHERE id=?", (time.time(), device_id))
        db.x("DELETE FROM sessions WHERE device_id=?", (device_id,))
    wireguard.sync()
    db.audit(actor.audit_name, "device.revoked", device_id=device_id, name=dev["name"])
    return {"ok": True}


@router.get("/devices/self/wireguard")
def my_wireguard(actor: Actor = Depends(current_actor)):
    if not actor.device_id:
        raise HTTPException(400, "Only paired devices have a tunnel profile")
    dev = db.q1("SELECT wg_ip FROM devices WHERE id=?", (actor.device_id,))
    return wireguard.client_profile(dev["wg_ip"]) if dev and dev["wg_ip"] else {"enabled": False}


# ---- API tokens for AI agents / MCP --------------------------------------------------------

class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scopes: list[str] = ["read"]


@router.get("/tokens")
def list_tokens(actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    rows = db.q("SELECT id, name, scopes, created_at, last_used, revoked_at FROM api_tokens ORDER BY id DESC")
    for r in rows:
        r["scopes"] = json.loads(r["scopes"])
    return rows


@router.post("/tokens")
def create_token(body: TokenBody, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    scopes = sorted({s for s in body.scopes if s in ("read", "write", "admin")} | {"read"})
    tok = new_token("pdk_ai")
    tid = db.x("INSERT INTO api_tokens(name, token_hash, scopes, user_id, created_at) VALUES(?,?,?,?,?)",
               (body.name.strip(), token_hash(tok), json.dumps(scopes), actor.user_id, time.time())).lastrowid
    db.audit(actor.audit_name, "token.created", token_id=tid, name=body.name, scopes=scopes)
    return {"id": tid, "token": tok, "scopes": scopes,
            "note": "Copy this token now — Paddock only stores a hash of it."}


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: int, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    db.x("UPDATE api_tokens SET revoked_at=? WHERE id=?", (time.time(), token_id))
    db.audit(actor.audit_name, "token.revoked", token_id=token_id)
    return {"ok": True}
