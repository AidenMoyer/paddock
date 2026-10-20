"""Remote access, linked servers, encrypted backup — admin API, plus the server-to-server peer API."""

from __future__ import annotations

import json
import time
from pathlib import Path

import segno
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .. import backup, catalog, db, federation, jobs, portmap, scanner, tls, vault, wireguard
from ..actor import Actor
from ..config import settings
from ..security import pairing_throttle, verify_password
from .auth import lan_urls
from .deps import client_ip, require_admin, require_human

router = APIRouter()


# =============================== remote access ===============================

@router.get("/api/admin/remote")
def remote_status(actor: Actor = Depends(require_admin)):
    pub = portmap.public_ip()
    _, wg_pub = wireguard.server_keys() if settings.wg_enabled else (None, None)
    cfg = db.get_setting("remote", {})
    return {
        "wireguard": {"enabled": settings.wg_enabled, "port": settings.wg_port, "subnet": str(settings.wg_subnet),
                      "server_ip": settings.wg_server_ip, "server_public_key": wg_pub,
                      "peers": db.q1("SELECT COUNT(*) c FROM devices WHERE wg_pubkey IS NOT NULL AND revoked_at IS NULL")["c"]},
        "tunnel": wireguard.status(),
        "auto_portmap": bool(cfg.get("auto_portmap")),
        "endpoint_host": cfg.get("endpoint_host", ""),
        "portmap": portmap.state(),
        "public_ip": pub,
        "cgnat": portmap.is_cgnat(pub),
        "endpoint_effective": wireguard.endpoint() if settings.wg_enabled else None,
        "lan_ip": portmap.lan_ip(),
        "lan_urls": lan_urls(),
        "fingerprint": tls.server_fingerprint(),
        "allowed_networks": [str(n) for n in settings.lan_cidrs],
    }


class RemoteBody(BaseModel):
    auto_portmap: bool | None = None
    endpoint_host: str | None = Field(default=None, max_length=255)


@router.put("/api/admin/remote")
async def remote_update(body: RemoteBody, actor: Actor = Depends(require_admin)):
    cfg = db.get_setting("remote", {})
    if body.endpoint_host is not None:
        host = body.endpoint_host.strip()
        if host and not all(c.isalnum() or c in ".-:[]" for c in host):
            raise HTTPException(400, "That doesn't look like a hostname or IP")
        cfg["endpoint_host"] = host
    if body.auto_portmap is not None:
        cfg["auto_portmap"] = body.auto_portmap
    db.set_setting("remote", cfg)
    if body.auto_portmap:
        await run_in_threadpool(portmap.refresh)
    elif body.auto_portmap is False:
        await run_in_threadpool(portmap.release)
    wireguard.sync()
    db.audit(actor.audit_name, "settings.remote", **body.model_dump(exclude_none=True))
    return {"ok": True}


# =============================== linked servers (admin) ===============================

def _peer_public(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "role": p["role"], "fingerprint": p["node_id"][:32],
            "tunnel_ip": p["tunnel_ip"], "created_at": p["created_at"], "last_contact": p["last_contact"],
            "last_error": p["last_error"], "our_grants": json.loads(p["our_grants"]),
            "their_grants": json.loads(p["their_grants"]), "vault_used": vault.used_bytes(p["id"])}


@router.get("/api/federation")
def federation_overview(actor: Actor = Depends(require_admin)):
    me = federation.self_info()
    cfg = backup.config()
    last = db.get_setting("backup_last")
    return {"self": {"name": me["name"], "role": me["role"], "fingerprint": me["node_id"][:32]},
            "peers": [_peer_public(p) for p in db.q("SELECT * FROM peers ORDER BY name")],
            "backup": cfg | {"last_run": last, "key_created": backup._key_path().exists()}}


class InviteBody(BaseModel):
    backup_quota_gb: float = Field(default=0, ge=0, le=1_000_000)
    libraries: list[int] = []


@router.post("/api/federation/invite")
def create_invite(body: InviteBody, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    inv = federation.create_invite(actor.user_id, body.model_dump())
    db.audit(actor.audit_name, "federation.invite_created", quota=body.backup_quota_gb, libraries=body.libraries)
    return inv | {"qr_svg": segno.make(inv["invite"], error="l").svg_inline(scale=3, dark="#0f0f0f", light="#ffffff", border=4, omitsize=True)}


class InviteText(BaseModel):
    invite: str = Field(max_length=8000)


@router.post("/api/federation/inspect")
def inspect(body: InviteText, actor: Actor = Depends(require_admin)):
    try:
        return federation.inspect_invite(body.invite)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/federation/accept")
async def accept(body: InviteText, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    try:
        res = await run_in_threadpool(federation.accept_invite, body.invite)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))
    except ConnectionError as e:
        raise HTTPException(502, str(e))
    federation.refresh_peer_ips()
    db.audit(actor.audit_name, "federation.joined", peer=res["id"], name=res["name"])
    return res


class GrantsBody(BaseModel):
    grants: dict
    name: str | None = Field(default=None, max_length=80)


@router.patch("/api/federation/peers/{pid}")
async def update_peer(pid: int, body: GrantsBody, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    p = db.q1("SELECT * FROM peers WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "No such linked server")
    grants = federation._clean_grants(body.grants)
    db.x("UPDATE peers SET our_grants=?, name=COALESCE(?, name) WHERE id=?", (json.dumps(grants), body.name, pid))
    db.audit(actor.audit_name, "federation.grants", peer=pid, **grants)
    try:
        await run_in_threadpool(federation.push_grants, pid)
    except ConnectionError:
        pass  # delivered on the next heartbeat
    return {"ok": True}


@router.delete("/api/federation/peers/{pid}")
async def delete_peer(pid: int, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    try:
        await run_in_threadpool(federation.unlink, pid)
    except KeyError:
        raise HTTPException(404, "No such linked server")
    return {"ok": True}


@router.post("/api/federation/peers/{pid}/ping")
async def ping(pid: int, actor: Actor = Depends(require_admin)):
    try:
        return await run_in_threadpool(federation.hello, pid)
    except (ConnectionError, KeyError) as e:
        return {"ok": False, "error": str(e)}


# --- browsing libraries another server shares with us (proxied, so phones can use it too) ---

@router.get("/api/federation/peers/{pid}/items")
async def peer_items(pid: int, request: Request, actor: Actor = Depends(require_admin)):
    c = federation.client(pid)
    r = await run_in_threadpool(c.request, "GET", f"/api/peer/items?{request.url.query}")
    return JSONResponse(r.json(), status_code=r.status_code)


@router.get("/api/federation/peers/{pid}/items/{rest:path}")
async def peer_item_proxy(pid: int, rest: str, request: Request, actor: Actor = Depends(require_admin)):
    if ".." in rest:
        raise HTTPException(400, "Bad path")
    c = federation.client(pid)
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("range", "if-range")}
    q = f"?{request.url.query}" if request.url.query else ""
    client, resp = await run_in_threadpool(c.stream, "GET", f"/api/peer/items/{rest}{q}", headers=headers)
    passthrough = {k: v for k, v in resp.headers.items() if k.lower() in (
        "content-type", "content-length", "content-range", "accept-ranges", "cache-control", "etag", "last-modified")}

    async def body():
        try:
            for chunk in resp.iter_bytes(256 * 1024):
                yield chunk
        finally:
            resp.close()
            client.close()

    return StreamingResponse(body(), status_code=resp.status_code, headers=passthrough)


# =============================== backups (admin) ===============================

@router.put("/api/backup/config")
def backup_config(body: dict, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    cfg = backup.save_config(body)
    backup.master_key()  # make sure the key exists before the first run
    db.audit(actor.audit_name, "backup.config", enabled=cfg["enabled"], targets=cfg["targets"], libraries=cfg["libraries"])
    return cfg


@router.post("/api/backup/run")
def backup_now(actor: Actor = Depends(require_admin)):
    if not backup.config()["targets"]:
        raise HTTPException(409, "Choose at least one linked server to back up to")
    return {"job_id": jobs.submit("backup", "all", lambda ctx: backup.run_all(ctx))}


@router.get("/api/backup/snapshots")
async def backup_snapshots(peer: int, actor: Actor = Depends(require_admin)):
    try:
        return await run_in_threadpool(backup.snapshots, peer)
    except ConnectionError as e:
        raise HTTPException(502, str(e))


class RestoreBody(BaseModel):
    peer: int
    snapshot: str
    library: str | None = None
    prefix: str | None = None
    recovery_key: str | None = None


@router.post("/api/backup/restore")
def backup_restore(body: RestoreBody, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    dest = settings.data_dir / "restores" / f"Restored {body.snapshot}"
    master = backup.key_from_recovery(body.recovery_key) if body.recovery_key else None
    job = jobs.submit("restore", body.snapshot, lambda ctx: backup.restore(body.peer, body.snapshot, dest, body.library,
                                                                         body.prefix, ctx, master), dedupe=False)
    db.audit(actor.audit_name, "backup.restore_started", **body.model_dump(exclude={"recovery_key"}))
    return {"job_id": job, "dest": str(dest)}


class VerifyBody(BaseModel):
    peer: int
    snapshot: str


@router.post("/api/backup/verify")
def backup_verify(body: VerifyBody, actor: Actor = Depends(require_admin)):
    return {"job_id": jobs.submit("verify", body.snapshot, lambda ctx: backup.verify(body.peer, body.snapshot, ctx=ctx))}


class PasswordBody(BaseModel):
    password: str = Field(max_length=200)


@router.post("/api/backup/recovery-key")
def show_recovery_key(body: PasswordBody, request: Request, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    ip = client_ip(request)
    if pairing_throttle.retry_after(f"rk:{ip}") > 0:
        raise HTTPException(429, "Too many attempts")
    user = db.q1("SELECT pw_hash FROM users WHERE id=?", (actor.user_id,))
    if not verify_password(body.password, user["pw_hash"]):
        pairing_throttle.fail(f"rk:{ip}")
        raise HTTPException(401, "Wrong password")
    db.audit(actor.audit_name, "backup.recovery_key_viewed", ip=ip)
    return {"key": backup.recovery_key()}


# =============================== peer API (server ↔ server) ===============================

def peer_actor(request: Request) -> dict:
    h = request.headers.get("authorization", "")
    p = federation.authenticate(h[7:].strip() if h.lower().startswith("bearer ") else "")
    if not p:
        raise HTTPException(401, "Unknown server")
    db.x("UPDATE peers SET last_contact=? WHERE id=?", (time.time(), p["id"]))
    return p


class HandshakeBody(BaseModel):
    code: str = Field(max_length=100)
    name: str = Field(max_length=80)
    node_id: str = Field(min_length=64, max_length=64)
    role: str = Field(default="server", max_length=10)
    ca_pem: str = Field(max_length=10000)
    urls: list[str] = Field(default=[], max_length=20)
    wg_pubkey: str | None = Field(default=None, max_length=64)
    tunnel_ip: str | None = Field(default=None, max_length=45)
    wg_endpoint: str | None = Field(default=None, max_length=300)
    api_port: int | None = None
    token: str = Field(min_length=20, max_length=200)


@router.post("/api/peer/handshake")
def peer_handshake(body: HandshakeBody, request: Request):
    ip = client_ip(request)
    if pairing_throttle.retry_after(f"peer:{ip}") > 0:
        raise HTTPException(429, "Too many attempts")
    try:
        return federation.handle_handshake(body.model_dump(), ip)
    except PermissionError as e:
        pairing_throttle.fail(f"peer:{ip}")
        raise HTTPException(403, str(e))


@router.post("/api/peer/hello")
def peer_hello(body: dict, p: dict = Depends(peer_actor)):
    return federation.handle_hello(p, body)


@router.post("/api/peer/grants")
def peer_grants(body: dict, p: dict = Depends(peer_actor)):
    g = body.get("grants") or {}
    db.x("UPDATE peers SET their_grants=? WHERE id=?",
         (json.dumps({"backup_quota_gb": float(g.get("backup_quota_gb") or 0), "libraries": list(g.get("libraries") or [])}), p["id"]))
    return {"ok": True}


@router.post("/api/peer/unlink")
def peer_unlink(p: dict = Depends(peer_actor)):
    federation.unlink(p["id"], notify=False)
    return {"ok": True}


# --- vault (they back up to us) ---

def _vault_peer(p: dict = Depends(peer_actor)) -> dict:
    if vault.quota_bytes(p) <= 0:
        raise HTTPException(403, "This server doesn't offer you backup space")
    return p


@router.post("/api/peer/vault/chunks/check")
def vault_check(body: dict, p: dict = Depends(_vault_peer)):
    return {"missing": vault.check(p, list(body.get("ids") or []))}


@router.put("/api/peer/vault/chunks/{cid}")
async def vault_put(cid: str, request: Request, p: dict = Depends(_vault_peer)):
    data = await request.body()
    try:
        await run_in_threadpool(vault.put_chunk, p, cid, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@router.get("/api/peer/vault/chunks/{cid}")
def vault_get(cid: str, p: dict = Depends(peer_actor)):
    try:
        return FileResponse(vault.get_chunk(p, cid), media_type="application/octet-stream")
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Not found")


@router.get("/api/peer/vault/chunks")
def vault_list(after: str = "", limit: int = 5000, p: dict = Depends(peer_actor)):
    return vault.list_chunks(p, after, limit)


@router.post("/api/peer/vault/chunks/delete")
def vault_delete(body: dict, p: dict = Depends(peer_actor)):
    return {"deleted": vault.delete_chunks(p, list(body.get("ids") or []))}


@router.get("/api/peer/vault/snapshots")
def vault_snapshots(p: dict = Depends(peer_actor)):
    return vault.list_snapshots(p)


@router.put("/api/peer/vault/snapshots/{name}")
async def vault_put_snapshot(name: str, request: Request, p: dict = Depends(_vault_peer)):
    data = await request.body()
    try:
        await run_in_threadpool(vault.put_snapshot, p, name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"ok": True}


@router.get("/api/peer/vault/snapshots/{name}")
def vault_get_snapshot(name: str, p: dict = Depends(peer_actor)):
    try:
        return FileResponse(vault.get_snapshot(p, name), media_type="application/octet-stream")
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Not found")


@router.delete("/api/peer/vault/snapshots/{name}")
def vault_delete_snapshot(name: str, p: dict = Depends(peer_actor)):
    vault.delete_snapshot(p, name)
    return {"ok": True}


@router.get("/api/peer/vault/usage")
def vault_usage(p: dict = Depends(peer_actor)):
    return vault.usage(p)


# --- shared libraries (read-only) ---

def _shared(p: dict) -> list[int]:
    return [int(x) for x in json.loads(p["our_grants"]).get("libraries", [])]


def _peer_actor_obj(p: dict) -> Actor:
    return Actor(user_id=0, username=f"peer:{p['name']}", is_admin=False, kind="token", scopes={"read"}, label=f"server:{p['name']}")


@router.get("/api/peer/libraries")
def peer_libraries(p: dict = Depends(peer_actor)):
    ids = _shared(p)
    if not ids:
        return []
    return db.q(f"SELECT id, name, kind, item_count FROM libraries WHERE id IN ({','.join('?' * len(ids))})", ids)


@router.get("/api/peer/items")
def peer_search(query: str | None = None, kind: str | None = None, library_id: int | None = None, limit: int = 60,
                offset: int = 0, p: dict = Depends(peer_actor)):
    ids = _shared(p)
    if library_id and library_id not in ids:
        raise HTTPException(403, "Not shared with you")
    out = []
    for lid in ([library_id] if library_id else ids):
        out += catalog.search(query=query, kind=kind.split(",") if kind else None, library_id=lid, limit=min(limit, 200),
                              offset=offset, sort="added" if not query else "relevance")
    for c in out:
        c.pop("path", None)
    return out[:limit]


def _shared_item(p: dict, item_id: int) -> dict:
    item = db.q1("SELECT * FROM items WHERE id=? AND trashed_at IS NULL", (item_id,))
    if not item or item["library_id"] not in _shared(p):
        raise HTTPException(404, "Not found")
    return item


@router.get("/api/peer/items/{item_id}")
def peer_item(item_id: int, p: dict = Depends(peer_actor)):
    _shared_item(p, item_id)
    it = catalog.get_item(item_id)
    it.pop("path", None)
    it.pop("projects", None)
    return it


@router.get("/api/peer/items/{item_id}/thumb")
def peer_thumb(item_id: int, p: dict = Depends(peer_actor)):
    from . import media

    _shared_item(p, item_id)
    return media.thumb(item_id, _peer_actor_obj(p))


@router.get("/api/peer/items/{item_id}/file")
def peer_file(item_id: int, p: dict = Depends(peer_actor)):
    from . import media

    _shared_item(p, item_id)
    return media.original(item_id, False, _peer_actor_obj(p))


@router.get("/api/peer/items/{item_id}/hls/master.m3u8")
def peer_hls_master(item_id: int, codecs: str | None = None, p: dict = Depends(peer_actor)):
    from . import media

    _shared_item(p, item_id)
    return media.hls_master(item_id, codecs, _peer_actor_obj(p))


@router.get("/api/peer/items/{item_id}/hls/{profile}/index.m3u8")
async def peer_hls_index(item_id: int, profile: str, p: dict = Depends(peer_actor)):
    from . import media

    _shared_item(p, item_id)
    return await media.hls_index(item_id, profile, _peer_actor_obj(p))


@router.get("/api/peer/items/{item_id}/hls/{profile}/seg_{seg}.ts")
async def peer_hls_segment(item_id: int, profile: str, seg: int, p: dict = Depends(peer_actor)):
    from . import media

    _shared_item(p, item_id)
    return await media.hls_segment(item_id, profile, seg, _peer_actor_obj(p))
