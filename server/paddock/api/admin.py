"""Server management for the web admin panel: health, settings, AI, transcoding, network, jobs, audit."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import __version__, ai, catalog, db, jobs, printers, red, scanner, tls, tools, transcode, transcribe, wireguard
from ..actor import Actor
from ..config import settings
from .auth import lan_urls, server_name
from .deps import current_actor, require_admin, require_human, require_write

router = APIRouter(prefix="/api")


# ---- overview ------------------------------------------------------------------------------

@router.get("/admin/overview")
def overview(actor: Actor = Depends(require_admin)):
    status = tools.TOOLS["server_status"].fn(actor, {})
    return {
        "server": {"name": server_name(), "version": __version__, "data_dir": str(settings.data_dir),
                   "media_roots": [str(r) for r in settings.media_roots]},
        "status": status,
        "stats": catalog.stats(),
        "libraries": db.q("SELECT id, name, kind, item_count, scan_state, last_scan FROM libraries ORDER BY name"),
        "hardware": transcode.detect_hardware(),
        "hwaccel_active": transcode.active_hwaccel(),
        "pipeline": transcode.pipeline_label(),
        "pipeline_short": transcode.pipeline_label(short=True),
        "transcode": {"settings": transcode.transcode_settings(), "cache": transcode.cache_usage(),
                      "active": transcode.segments.status()},
        "ai": ai.public_config() | {"status": ai.server_status()},
        "stt": transcribe.config() | {"status": transcribe.server_status()},
        "users": db.q1("SELECT COUNT(*) c FROM users")["c"],
        "devices": db.q1("SELECT COUNT(*) c FROM devices WHERE revoked_at IS NULL")["c"],
        "sessions": db.q1("SELECT COUNT(*) c FROM sessions WHERE expires_at IS NULL OR expires_at>?", (time.time(),))["c"],
        "network": network_info(),
        "jobs": jobs.recent(15),
    }


@router.get("/jobs")
def list_jobs(actor: Actor = Depends(current_actor)):
    return {"active": jobs.active(), "recent": jobs.recent(40)}


@router.get("/admin/audit")
def audit(limit: int = 200, before: int | None = None, actor: Actor = Depends(require_admin)):
    sql = "SELECT * FROM audit_log"
    params: list = []
    if before:
        sql += " WHERE id<?"
        params.append(before)
    rows = db.q(sql + " ORDER BY id DESC LIMIT ?", params + [min(limit, 1000)])
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return rows


@router.get("/admin/sessions")
def sessions(actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    return db.q("SELECT substr(s.token_hash,1,12) AS id, s.kind, s.created_at, s.last_seen, s.ip, s.expires_at, "
                "u.username, d.name AS device FROM sessions s JOIN users u ON u.id=s.user_id "
                "LEFT JOIN devices d ON d.id=s.device_id ORDER BY s.last_seen DESC LIMIT 500")


@router.delete("/admin/sessions/{prefix}")
def end_session(prefix: str, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    if len(prefix) < 12:
        raise HTTPException(400, "Bad id")
    db.x("DELETE FROM sessions WHERE token_hash LIKE ? AND kind='web'", (prefix + "%",))
    db.audit(actor.audit_name, "session.ended", id=prefix)
    return {"ok": True}


# ---- general settings -------------------------------------------------------------------------

class GeneralBody(BaseModel):
    server_name: str | None = Field(default=None, max_length=60)
    lan_urls: list[str] | None = None


@router.put("/admin/general")
def general(body: GeneralBody, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    if body.server_name:
        db.set_setting("server_name", body.server_name.strip())
    if body.lan_urls is not None:
        urls = [u.strip().rstrip("/") for u in body.lan_urls if u.strip().startswith(("https://", "http://"))]
        db.set_setting("lan_urls", urls or None)
    db.audit(actor.audit_name, "settings.general", **body.model_dump(exclude_none=True))
    return {"ok": True}


# ---- network & security -------------------------------------------------------------------------

def network_info() -> dict:
    _, pub = wireguard.server_keys() if settings.wg_enabled else (None, None)
    return {
        "lan_urls": lan_urls(),
        "tls": settings.tls,
        "fingerprint": tls.server_fingerprint(),
        "pin": tls.spki_pin(),
        "allowed_networks": [str(n) for n in settings.lan_cidrs],
        "wireguard": {
            "enabled": settings.wg_enabled,
            "endpoint": settings.wg_endpoint,
            "port": settings.wg_port,
            "subnet": str(settings.wg_subnet),
            "server_ip": settings.wg_server_ip,
            "server_public_key": pub,
            "peers": db.q1("SELECT COUNT(*) c FROM devices WHERE wg_pubkey IS NOT NULL AND revoked_at IS NULL")["c"],
            "router_rule": f"Forward UDP {settings.wg_port} → this server's LAN IP, port {settings.wg_port}. Nothing else.",
        },
    }


@router.get("/admin/network")
def network(actor: Actor = Depends(require_admin)):
    return network_info()


@router.get("/ca.crt")
def ca_cert():
    """Install on desktops to remove the browser certificate warning. Public information."""
    pem = tls.ca_pem()
    if not pem:
        raise HTTPException(404, "TLS is off")
    return Response(pem, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="paddock-ca.crt"'})


# ---- transcoding -----------------------------------------------------------------------------------

class TranscodeBody(BaseModel):
    hwaccel: str | None = None
    cache_gb: float | None = Field(default=None, ge=1, le=10000)
    ahead_segments: int | None = Field(default=None, ge=5, le=200)


@router.put("/admin/transcode")
def set_transcode(body: TranscodeBody, actor: Actor = Depends(require_admin)):
    if body.hwaccel and body.hwaccel not in ("auto", "none", "nvenc", "qsv", "vaapi"):
        raise HTTPException(400, "hwaccel must be auto, none, nvenc, qsv or vaapi")
    cfg = db.get_setting("transcode", {})
    cfg.update(body.model_dump(exclude_none=True))
    db.set_setting("transcode", cfg)
    db.audit(actor.audit_name, "settings.transcode", **cfg)
    return {"settings": transcode.transcode_settings(), "active": transcode.pipeline_label()}


@router.post("/admin/transcode/detect")
def detect(actor: Actor = Depends(require_admin)):
    return transcode.detect_hardware(force=True) | {"active": transcode.pipeline_label()}


@router.post("/admin/transcode/clear-cache")
def clear_cache(actor: Actor = Depends(require_admin)):
    transcode.clear_cache()
    db.audit(actor.audit_name, "transcode.cache_cleared")
    return transcode.cache_usage()


@router.post("/libraries/{lib_id}/optimize")
def optimize_library(lib_id: int, profiles: str = "1080p,720p", actor: Actor = Depends(require_admin)):
    """Pre-encode every video in a library that can't direct-play on phones."""
    wanted = [p for p in profiles.split(",") if p in transcode.PROFILES]
    items = db.q("SELECT * FROM items WHERE library_id=? AND kind='video' AND trashed_at IS NULL", (lib_id,))

    def run(ctx):
        n = 0
        for i, item in enumerate(items):
            meta = json.loads(item["meta"] or "{}")
            if transcode.can_direct(item, meta, {"h264", "aac", "mp3"}):
                continue
            ctx.progress(i / max(len(items), 1), f"Optimizing {item['title']}")
            try:
                transcode.optimize(item, meta, scanner.safe_item_path(item), wanted)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        return f"Optimized {n} videos"

    return {"job_id": jobs.submit("optimize-library", str(lib_id), run)}


# ---- AI ------------------------------------------------------------------------------------------------

@router.get("/admin/ai")
def get_ai(actor: Actor = Depends(require_admin)):
    return ai.public_config() | {"status": ai.server_status()}


@router.put("/admin/ai")
def put_ai(body: dict, actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    try:
        cfg = ai.save_config(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(actor.audit_name, "settings.ai", provider=cfg["provider"], model=cfg.get("model"))
    return cfg | {"status": ai.server_status()}


class ModelBody(BaseModel):
    model: str = Field(min_length=1, max_length=200)


@router.post("/admin/ai/pull")
def pull(body: ModelBody, actor: Actor = Depends(require_admin)):
    return {"job_id": jobs.submit("ai-pull", body.model, lambda ctx: ai.pull_model(body.model, ctx))}


@router.post("/admin/ai/unload")
def unload(actor: Actor = Depends(require_admin)):
    ai.unload_model()
    return ai.server_status()


@router.post("/admin/ai/warm")
def warm(actor: Actor = Depends(require_admin)):
    return {"job_id": jobs.submit("ai-warm", "model", lambda ctx: (ai.warm_up(), "Model loaded")[1])}


# ---- transcription --------------------------------------------------------------------------------------

@router.get("/admin/stt")
def get_stt(actor: Actor = Depends(require_admin)):
    return transcribe.config() | {"status": transcribe.server_status()}


@router.put("/admin/stt")
def put_stt(body: dict, actor: Actor = Depends(require_admin)):
    try:
        cfg = transcribe.save_config(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(actor.audit_name, "settings.stt", engine=cfg["engine"])
    return cfg | {"status": transcribe.server_status()}


@router.post("/admin/stt/download")
def stt_download(body: ModelBody, actor: Actor = Depends(require_admin)):
    return {"job_id": jobs.submit("stt-download", body.model, lambda ctx: transcribe.download_server_model(body.model))}


class WhisperModelBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@router.post("/admin/stt/whisper-model")
def stt_whisper_download(body: WhisperModelBody, actor: Actor = Depends(require_admin)):
    if body.name not in transcribe.WHISPER_MODELS:
        raise HTTPException(400, "Unknown model")
    db.audit(actor.audit_name, "stt.model_download", model=body.name)
    return {"job_id": jobs.submit("stt-model", body.name, lambda ctx: transcribe.download_whisper_model(body.name, ctx))}


@router.put("/admin/stt/whisper-model")
def stt_whisper_use(body: WhisperModelBody, actor: Actor = Depends(require_admin)):
    try:
        cfg = transcribe.use_whisper_model(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(actor.audit_name, "settings.stt", model=body.name)
    return cfg | {"status": transcribe.server_status()}


@router.post("/libraries/{lib_id}/transcribe")
def transcribe_library(lib_id: int, actor: Actor = Depends(require_admin)):
    if not transcribe.enabled():
        raise HTTPException(409, "Transcription isn't set up")
    n = 0
    for item in db.q("SELECT * FROM items WHERE library_id=? AND kind IN ('video','red_clip') AND trashed_at IS NULL",
                     (lib_id,)):
        n += transcribe.transcribe_all(item)
    return {"queued_chunks": n}


@router.post("/libraries/{lib_id}/autotag")
def autotag_library(lib_id: int, only_untagged: bool = True, actor: Actor = Depends(require_admin)):
    sql = "SELECT id FROM items WHERE library_id=? AND thumb_state='ok' AND trashed_at IS NULL"
    if only_untagged:
        sql += " AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.item_id=items.id AND t.source='ai')"
    ids = [r["id"] for r in db.q(sql, (lib_id,))]
    return {"job_id": jobs.submit("autotag", f"library:{lib_id}", lambda ctx: ai.autotag(ids, ctx)), "items": len(ids)}


# ---- printers -------------------------------------------------------------------------------------------

@router.get("/printers")
def get_printers(actor: Actor = Depends(current_actor)):
    return printers.public_view()


@router.put("/admin/printers")
def put_printers(body: list[dict], actor: Actor = Depends(require_admin), _h: Actor = Depends(require_human)):
    existing = {p["name"]: p for p in printers.configured()}
    out = []
    try:
        for p in body:
            if not p.get("api_key") and p.get("name") in existing:
                p["api_key"] = existing[p["name"]].get("api_key", "")  # keep secret if not re-entered
            out.append(printers.validate(p))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.set_setting("printers", out)
    return printers.public_view()


class PrintBody(BaseModel):
    printer: str
    start: bool = False


@router.post("/items/{item_id}/print")
def print_item(item_id: int, body: PrintBody, actor: Actor = Depends(require_write)):
    try:
        return printers.send(item_id, body.printer, body.start)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---- RED / cinema ---------------------------------------------------------------------------------------

class OffloadBody(BaseModel):
    source: str = Field(max_length=1000)
    library_id: int
    label: str = Field(default="Shoot", max_length=80)


@router.post("/red/offload")
def offload(body: OffloadBody, actor: Actor = Depends(require_admin)):
    """Copy a camera card (mounted/copied somewhere under the media roots) into a RED library, verified."""
    src = Path(body.source).resolve()
    lib = db.q1("SELECT * FROM libraries WHERE id=?", (body.library_id,))
    if not lib:
        raise HTTPException(404, "No such library")
    if not scanner.allowed_root(src) or not src.is_dir():
        raise HTTPException(400, "Source must be a folder inside the media roots")
    if scanner.within(src, Path(lib["path"])):
        raise HTTPException(400, "Source is already inside the library")

    def run(ctx):
        res = red.offload(src, Path(lib["path"]), body.label, ctx.progress)
        db.audit(actor.audit_name, "red.offload", **res)
        scanner.queue_scan(lib["id"])
        return f"Copied and verified {res['files']} files → {res['dest']}"

    return {"job_id": jobs.submit("offload", str(src), run)}


@router.post("/items/{item_id}/proxy")
def make_proxy(item_id: int, actor: Actor = Depends(require_admin)):
    item = db.q1("SELECT * FROM items WHERE id=? AND kind='red_clip'", (item_id,))
    if not item:
        raise HTTPException(404, "Not a RED clip")
    meta = json.loads(item["meta"] or "{}")

    def run(ctx):
        ctx.progress(0.05, "Rendering proxy with REDline")
        p = red.make_proxy(Path(item["path"]), meta.get("clip_name") or Path(item["path"]).stem)
        if not p:
            return "REDline isn't installed in the container (see docs/RED.md)"
        meta["proxy"] = str(p)
        db.x("UPDATE items SET meta=?, thumb_state='pending' WHERE id=?", (json.dumps(meta, default=str), item_id))
        scanner.queue_scan(item["library_id"])
        return f"Proxy ready: {p.name}"

    return {"job_id": jobs.submit("proxy", str(item_id), run)}
