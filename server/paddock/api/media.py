"""Browsing, artwork, playback and progress."""

from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import access, catalog, db, kinds, thumbs, transcode
from ..actor import Actor
from ..scanner import safe_item_path
from .deps import current_actor, require_admin, require_write

router = APIRouter(prefix="/api")

CACHE_FOREVER = {"Cache-Control": "private, max-age=31536000, immutable"}


def _item_or_404(item_id: int, actor: Actor | None, include_trashed: bool = False) -> dict:
    """The item, if it exists and this person may see it (same answer either way, so nothing leaks)."""
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item or (item["trashed_at"] and not include_trashed) or not access.can_read_item(actor, item):
        raise HTTPException(404, "Not found")
    return item


def _writable_or_403(item: dict, actor: Actor) -> None:
    if not access.can_write_library(actor, item["library_id"]):
        raise HTTPException(403, "You can look at this, but not change it")


# ---- feeds ------------------------------------------------------------------------------

@router.get("/home")
def home(actor: Actor = Depends(current_actor)):
    rows = []
    allowed = access.library_ids(actor)
    media_only = not actor.is_admin and access.media_only(actor.user_id)

    def add(key, title, items, kind="items", **extra):
        if items:
            rows.append({"key": key, "title": title, "type": kind, "items": items, **extra})

    add("continue", "Continue watching", catalog.continue_watching(actor.user_id, allowed=allowed))
    add("new_movies", "New movies", [c for c in catalog.search(kind="video", sort="added", limit=60, allowed=allowed)
                                     if c["library_kind"] == "movies"][:20])
    shows = catalog.groups(prefix="show:", limit=20, allowed=allowed)
    add("shows", "TV shows", shows, kind="groups")
    add("home_videos", "Home videos", [c for c in catalog.search(kind="video", sort="newest", limit=80, allowed=allowed)
                                       if c["library_kind"] in ("home_videos", "photos", "mixed")][:20])
    add("memories", "On this day", catalog.on_this_day(20, allowed=allowed))
    add("photos", "Recent photos", catalog.search(kind="photo", sort="newest", limit=30, allowed=allowed))
    add("shoots", "Shoot days", catalog.groups(prefix="shoot:", limit=12, allowed=allowed), kind="groups")
    add("albums", "Music", catalog.groups(prefix="album:", limit=20, allowed=allowed), kind="groups")
    if not media_only:
        from .. import projects

        add("projects", "Projects", projects.listing(actor)[:12], kind="projects")
        add("engineering", "Recent CAD & prints", catalog.search(kind=["model3d", "gcode", "cad", "pcb"], sort="added",
                                                                   limit=20, allowed=allowed))
    return {"rows": rows, "stats": catalog.stats(allowed), "media_only": media_only}


@router.get("/items")
def list_items(query: str | None = None, kind: str | None = None, library_id: int | None = None,
               tag: str | None = None, group: str | None = None, project_id: int | None = None,
               favorite: bool | None = None, sort: str = "newest", limit: int = Query(60, le=500),
               offset: int = 0, date_from: float | None = None, date_to: float | None = None,
               actor: Actor = Depends(current_actor)):
    kinds_ = kind.split(",") if kind else None
    return catalog.search(query=query, kind=kinds_, library_id=library_id, tag=tag, group=group,
                          project_id=project_id, favorite=favorite, sort=sort if not query or sort != "newest" else "relevance",
                          limit=limit, offset=offset, date_from=date_from, date_to=date_to, allowed=access.library_ids(actor))


@router.get("/groups")
def list_groups(prefix: str | None = None, library_id: int | None = None, limit: int = Query(200, le=1000),
                offset: int = 0, actor: Actor = Depends(current_actor)):
    return catalog.groups(library_id=library_id, prefix=prefix, limit=limit, offset=offset, allowed=access.library_ids(actor))


@router.get("/timeline")
def timeline(library_id: int | None = None, kind: str = "photo,video", actor: Actor = Depends(current_actor)):
    """Month buckets for the photo timeline scrubber."""
    ks = kind.split(",")
    where = f"trashed_at IS NULL AND kind IN ({','.join('?' * len(ks))})"
    params: list = list(ks)
    if library_id:
        where += " AND library_id=?"
        params.append(library_id)
    frag, ids = access.clause(access.library_ids(actor), "library_id")
    where += frag
    params += ids
    return db.q(f"SELECT strftime('%Y-%m', taken_at, 'unixepoch', 'localtime') AS month, COUNT(*) AS count, "
                f"MIN(taken_at) AS first, MAX(taken_at) AS last FROM items WHERE {where} "
                f"GROUP BY month ORDER BY month DESC", params)


@router.get("/places")
def places(actor: Actor = Depends(current_actor)):
    """Geotagged items clustered on a ~1 km grid (no map tiles needed — no external services)."""
    frag, ids = access.clause(access.library_ids(actor), "library_id")
    rows = db.q("SELECT ROUND(lat, 2) AS lat, ROUND(lon, 2) AS lon, COUNT(*) AS count, MAX(id) AS cover_id, "
                f"MIN(taken_at) AS first, MAX(taken_at) AS last FROM items WHERE lat IS NOT NULL AND trashed_at IS NULL{frag} "
                "GROUP BY ROUND(lat, 2), ROUND(lon, 2) ORDER BY count DESC LIMIT 500", ids)
    return rows


@router.get("/tags")
def popular_tags(limit: int = 200, actor: Actor = Depends(current_actor)):
    frag, ids = access.clause(access.library_ids(actor))
    return db.q(f"SELECT tag, COUNT(*) AS count FROM tags t JOIN items i ON i.id=t.item_id WHERE i.trashed_at IS NULL{frag} "
                "GROUP BY tag ORDER BY count DESC LIMIT ?", (*ids, limit))


@router.get("/search/suggest")
def suggest(q: str, actor: Actor = Depends(current_actor)):
    if len(q.strip()) < 2:
        return {"items": [], "tags": []}
    allowed = access.library_ids(actor)
    items = catalog.search(query=q, limit=8, sort="relevance", allowed=allowed)
    frag, ids = access.clause(allowed)
    tags = db.q(f"SELECT tag, COUNT(*) c FROM tags t JOIN items i ON i.id=t.item_id WHERE tag LIKE ?{frag} "
                "GROUP BY tag ORDER BY c DESC LIMIT 6", (q.strip() + "%", *ids))
    return {"items": items, "tags": tags}


@router.get("/stats")
def stats(actor: Actor = Depends(current_actor)):
    return catalog.stats(access.library_ids(actor))


# ---- single item ------------------------------------------------------------------------------

@router.get("/items/{item_id}")
def item_detail(item_id: int, actor: Actor = Depends(current_actor)):
    _item_or_404(item_id, actor, include_trashed=actor.is_admin)
    item = catalog.get_item(item_id, actor.user_id, include_trashed=actor.is_admin)
    if not item:
        raise HTTPException(404, "Not found")
    if not actor.is_admin:
        item["meta"].pop("proxy", None)
    if item["group"]:
        item["related"] = catalog.search(group=item["group"], sort="group", limit=60, allowed=access.library_ids(actor))
    item["can_write"] = access.can_write_library(actor, item["library_id"])
    return item


@router.get("/items/{item_id}/up_next")
def up_next(item_id: int, limit: int = Query(30, le=100), actor: Actor = Depends(current_actor)):
    """What plays next: the rest of the show or album, then a random pick of similar things from the same kind
    of collection (movies lead to movies, songs to songs; see recommend.py). Videos and songs only."""
    from .. import recommend

    item = _item_or_404(item_id, actor)
    out = recommend.up_next(item, actor.user_id, access.library_ids(actor), limit)
    prog = {r["item_id"]: r for r in db.q(
        f"SELECT item_id, position, completed FROM progress WHERE user_id=? AND item_id IN ({','.join('?' * len(out)) or 'NULL'})",
        (actor.user_id, *[c["id"] for c in out]))}
    for c in out:
        if c["id"] in prog:
            c["progress"] = {"position": prog[c["id"]]["position"], "completed": bool(prog[c["id"]]["completed"])}
    return out[:limit]


class ItemPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    favorite: bool | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []


@router.patch("/items/{item_id}")
def update_item(item_id: int, body: ItemPatch, actor: Actor = Depends(require_write)):
    item = _item_or_404(item_id, actor)
    if body.title or body.add_tags or body.remove_tags:
        _writable_or_403(item, actor)
    with db.tx():
        if body.title:
            db.x("UPDATE items SET title=? WHERE id=?", (body.title.strip(), item_id))
        if body.favorite is not None:
            db.x("UPDATE items SET favorite=? WHERE id=?", (int(body.favorite), item_id))
        for t in body.add_tags:
            if t.strip():
                db.x("INSERT OR IGNORE INTO tags(item_id, tag, source) VALUES(?,?, 'user')", (item_id, t.strip()[:60]))
        for t in body.remove_tags:
            db.x("DELETE FROM tags WHERE item_id=? AND tag=?", (item_id, t))
        db.fts_index(item_id)
    return catalog.get_item(item_id, actor.user_id)


class IdsBody(BaseModel):
    ids: list[int] = Field(max_length=5000)


def _all_writable(ids: list[int], actor: Actor) -> None:
    acc = access.library_access(actor)
    if acc is None or not ids:
        return
    rows = db.q(f"SELECT library_id FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)
    if any(acc.get(r["library_id"]) != "write" for r in rows):
        raise HTTPException(403, "Some of these are in a library you can't change")


@router.post("/items/trash")
def trash_items(body: IdsBody, actor: Actor = Depends(require_write)):
    _all_writable(body.ids, actor)
    if body.ids:
        db.x(f"UPDATE items SET trashed_at=? WHERE id IN ({','.join('?' * len(body.ids))})", (time.time(), *body.ids))
        db.audit(actor.audit_name, "items.trashed", ids=body.ids[:100], count=len(body.ids))
    return {"trashed": len(body.ids)}


@router.post("/items/restore")
def restore_items(body: IdsBody, actor: Actor = Depends(require_write)):
    _all_writable(body.ids, actor)
    if body.ids:
        db.x(f"UPDATE items SET trashed_at=NULL WHERE id IN ({','.join('?' * len(body.ids))})", body.ids)
    return {"restored": len(body.ids)}


@router.get("/trash")
def list_trash(actor: Actor = Depends(current_actor)):
    frag, ids = access.clause(access.library_ids(actor))
    rows = db.q(f"SELECT {catalog.ITEM_COLS}, i.trashed_at FROM items i JOIN libraries l ON l.id=i.library_id "
                f"WHERE i.trashed_at IS NOT NULL{frag} ORDER BY i.trashed_at DESC LIMIT 1000", ids)
    return catalog.cards(rows)


class EmptyTrashBody(BaseModel):
    ids: list[int] = Field(max_length=5000)
    confirm: str


@router.post("/trash/empty")
def empty_trash(body: EmptyTrashBody, actor: Actor = Depends(require_admin)):
    """Permanently delete files. Human admins only (never API/agent tokens), with typed confirmation."""
    if actor.kind not in ("web", "device"):
        raise HTTPException(403, "Only a signed-in person can permanently delete files")
    if body.confirm != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm")
    deleted, failed = 0, []
    for iid in body.ids:
        item = db.q1("SELECT * FROM items WHERE id=? AND trashed_at IS NOT NULL", (iid,))
        if not item:
            continue
        try:
            p = safe_item_path(item)
            from .. import red

            targets = red.spans(p) if item["kind"] == "red_clip" else [p]
            for t in targets:
                t.unlink(missing_ok=True)
            thumbs.thumb_path(iid).unlink(missing_ok=True)
            db.fts_remove(iid)
            db.x("DELETE FROM items WHERE id=?", (iid,))
            deleted += 1
        except (OSError, PermissionError) as e:
            failed.append({"id": iid, "error": str(e)[:200]})
    db.audit(actor.audit_name, "trash.emptied", deleted=deleted, failed=len(failed))
    return {"deleted": deleted, "failed": failed}


# ---- artwork --------------------------------------------------------------------------------

@router.get("/items/{item_id}/thumb")
def thumb(item_id: int, actor: Actor = Depends(current_actor)):
    p = thumbs.thumb_path(item_id)
    if not p.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(p, media_type="image/webp", headers=CACHE_FOREVER)


@router.get("/items/{item_id}/preview")
def preview(item_id: int, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    if item["kind"] != "photo":
        raise HTTPException(400, "Not a photo")
    safe_item_path(item)
    # browsers display these formats natively — skip conversion
    if item["ext"] in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif") and item["size"] < 25_000_000:
        return FileResponse(item["path"], media_type=kinds.mime_for(item["ext"]), headers=CACHE_FOREVER)
    p = thumbs.preview(item)
    if not p:
        raise HTTPException(415, "Can't render this image")
    return FileResponse(p, media_type="image/webp", headers=CACHE_FOREVER)


@router.get("/items/{item_id}/file")
def original(item_id: int, download: bool = False, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    p = safe_item_path(item)
    if not p.exists():
        raise HTTPException(404, "File missing on disk — rescan the library")
    headers = {"Cache-Control": "private, max-age=3600"}
    return FileResponse(p, media_type=kinds.mime_for(item["ext"]), headers=headers,
                        filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@router.get("/items/{item_id}/mesh.stl")
def mesh(item_id: int, actor: Actor = Depends(current_actor)):
    """Any supported mesh (STL/OBJ/3MF) as binary STL for the browser 3D viewer."""
    item = _item_or_404(item_id, actor)
    if item["ext"] not in (".stl", ".obj", ".3mf"):
        raise HTTPException(400, "Not a mesh")
    p = safe_item_path(item)
    if item["ext"] == ".stl":
        return FileResponse(p, media_type="model/stl", headers={"Cache-Control": "private, max-age=3600"})
    from ..stlrender import load_mesh

    tris = load_mesh(p).astype(np.float32)
    buf = io.BytesIO()
    buf.write(b"Paddock mesh".ljust(80, b" "))
    buf.write(np.uint32(len(tris)).tobytes())
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["v"] = tris
    buf.write(rec.tobytes())
    return Response(buf.getvalue(), media_type="model/stl", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/items/{item_id}/gcode")
def gcode_text(item_id: int, actor: Actor = Depends(current_actor)):
    """Raw G-code for the in-browser toolpath preview (capped at 60 MB)."""
    item = _item_or_404(item_id, actor)
    if item["kind"] != "gcode" or item["ext"] == ".bgcode":
        raise HTTPException(400, "Not text G-code")
    if item["size"] > 60_000_000:
        raise HTTPException(413, "Too large to preview")
    return FileResponse(safe_item_path(item), media_type="text/plain")


# ---- playback ------------------------------------------------------------------------------

def _source_for(item: dict, meta: dict) -> Path:
    if item["kind"] == "red_clip":
        if not meta.get("proxy"):
            raise HTTPException(409, "This RED clip has no proxy yet")
        p = Path(meta["proxy"])
        if not p.exists():
            raise HTTPException(409, "Proxy file is missing")
        return p
    return safe_item_path(item)


def _sidecar_subs(path: Path) -> list[dict]:
    out = []
    for p in sorted(path.parent.glob(f"{path.stem}*")):
        if p.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa"):
            lang = p.stem[len(path.stem):].strip(".") or "default"
            out.append({"name": p.name, "lang": lang})
    return out


def _client_codecs(codecs: str | None) -> set[str] | None:
    return {c.strip().lower() for c in codecs.split(",") if c.strip()} if codecs else None


def _res_label(item: dict) -> str:
    h = min(item["width"] or 0, item["height"] or 0) or item["height"] or 0
    if not h:
        return "source"
    for name, (ph, _, _) in transcode.PROFILES.items():
        if h >= ph * 0.95:
            return name
    return f"{h}p"


@router.get("/items/{item_id}/playback")
def playback(item_id: int, codecs: str | None = None, actor: Actor = Depends(current_actor)):
    """How to play this item. `codecs` = what the client decodes, e.g. h264,hevc,av1,aac,opus,mkv.

    Returns every option the player's quality menu needs: Auto (HLS master), Original
    (direct file or stream-copy HLS) and each transcoded rung.
    """
    from .. import transcribe

    item = _item_or_404(item_id, actor)
    meta = json.loads(item["meta"] or "{}")
    if item["kind"] not in ("video", "audio", "red_clip", "braw_clip"):
        raise HTTPException(400, "Not playable")
    client = _client_codecs(codecs)
    plan = transcode.playback_plan(item, meta, client)
    base = f"/api/items/{item_id}"
    q = f"?codecs={codecs}" if codecs else ""
    plan["duration"] = item["duration"] or meta.get("duration")
    plan["width"], plan["height"] = item["width"], item["height"]
    if plan["mode"] == "unavailable":
        return plan
    qualities = []
    if plan["mode"] == "direct":
        plan["url"] = f"{base}/file"
        plan["mime"] = kinds.mime_for(item["ext"])
        qualities.append({"id": "original", "label": f"Original ({_res_label(item)})", "type": "direct",
                          "url": f"{base}/file", "mime": plan["mime"]})
    elif plan.get("original"):
        qualities.append({"id": "original", "label": f"Original ({_res_label(item)})", "type": "hls",
                          "url": f"{base}/hls/original/index.m3u8"})
    if item["kind"] in ("video", "red_clip"):
        plan["hls"] = f"{base}/hls/master.m3u8{q}"
        for p in plan.get("profiles", []):
            qualities.append({"id": p, "label": p, "type": "hls", "url": f"{base}/hls/{p}/index.m3u8",
                              "progressive": f"{base}/stream.mp4?profile={p}"})
        qualities.insert(0, {"id": "auto", "label": "Auto", "type": "hls", "url": plan["hls"]})
    elif item["kind"] == "audio" and plan["mode"] != "direct":
        plan["progressive"] = f"{base}/stream.mp4"
        qualities.append({"id": "aac", "label": "AAC 256k", "type": "progressive", "url": f"{base}/stream.mp4"})
    plan["qualities"] = qualities
    plan["default_quality"] = "original" if plan["mode"] == "direct" else "auto"
    subs = []
    if item["kind"] == "video":
        for s in meta.get("subtitle_streams", []):
            if s.get("codec") in ("subrip", "ass", "ssa", "mov_text", "webvtt"):
                subs.append({"label": s.get("lang") or f"Track {s['index']}", "url": f"{base}/subtitles/{s['index']}.vtt"})
        for s in _sidecar_subs(Path(item["path"])):
            subs.append({"label": s["lang"], "url": f"{base}/subtitles/file/{s['name']}"})
    plan["subtitles"] = subs
    plan["live_captions"] = {"enabled": transcribe.enabled(), "url": f"{base}/captions/live",
                             "vtt": f"{base}/captions.vtt", "complete": transcribe.load(item_id).get("complete", False)}
    tp = transcode.trickplay_info(item)
    plan["trickplay"] = {**tp, "base": f"{base}/trickplay/"} if tp else None
    if not tp and item["kind"] == "video" and plan["duration"] and plan["duration"] > 60:
        _queue_trickplay(item, meta)
    prog = db.q1("SELECT position, completed FROM progress WHERE user_id=? AND item_id=?", (actor.user_id, item_id))
    plan["resume_at"] = prog["position"] if prog and not prog["completed"] else 0
    plan["hwaccel"] = transcode.active_hwaccel()
    return plan


def _queue_trickplay(item: dict, meta: dict) -> None:
    from .. import jobs

    src = _source_for(item, meta)
    jobs.submit("trickplay", str(item["id"]),
                lambda ctx: "ok" if transcode.build_trickplay(item, meta, src) else "failed")


@router.get("/items/{item_id}/trickplay/{name}")
def trickplay_sprite(item_id: int, name: str, actor: Actor = Depends(current_actor)):
    if not name.startswith("sprite_") or not name.endswith(".jpg") or "/" in name or "\\" in name:
        raise HTTPException(404, "Not found")
    p = transcode.trickplay_dir(item_id) / name
    if not p.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="image/jpeg", headers=CACHE_FOREVER)


@router.get("/items/{item_id}/hls/master.m3u8")
def hls_master(item_id: int, codecs: str | None = None, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    meta = json.loads(item["meta"] or "{}")
    client = _client_codecs(codecs) or (kinds.BROWSER_VIDEO_CODECS | kinds.BROWSER_AUDIO_CODECS)
    # Auto is an H.264-only ladder: mixing the original codec (e.g. 10-bit HEVC) into one adaptive stream
    # forces decoder re-initialisation on every switch. "Original" stays a separate, explicit choice.
    return PlainTextResponse(transcode.master_playlist(meta, False),
                             media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"})


@router.get("/items/{item_id}/hls/{profile}/index.m3u8")
async def hls_index(item_id: int, profile: str, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    meta = json.loads(item["meta"] or "{}")
    if profile == "original":
        src = _source_for(item, meta)
        segs = await run_in_threadpool(transcode.keyframe_segments, src, item_id, item["mtime"])
        durations = [d for _, d in segs]
    else:
        if profile not in transcode.PROFILES:
            raise HTTPException(404, "Unknown profile")
        dur = item["duration"] or meta.get("duration")
        if not dur:
            raise HTTPException(409, "Unknown duration — can't build a playlist")
        durations = transcode.fixed_segments(dur)
    return PlainTextResponse(transcode.media_playlist(durations), media_type="application/vnd.apple.mpegurl",
                             headers={"Cache-Control": "no-cache"})


@router.get("/items/{item_id}/hls/{profile}/seg_{seg}.ts")
async def hls_segment(item_id: int, profile: str, seg: int, actor: Actor = Depends(current_actor)):
    if (profile not in transcode.PROFILES and profile != "original") or seg < 0:
        raise HTTPException(404, "Not found")
    item = _item_or_404(item_id, actor)
    meta = json.loads(item["meta"] or "{}")
    src = _source_for(item, meta)
    path = await run_in_threadpool(transcode.segments.segment, item, meta, src, profile, seg)
    if not path:
        raise HTTPException(503, "Transcoder couldn't produce this segment")
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/items/{item_id}/stream.mp4")
def progressive(item_id: int, start: float = 0, profile: str = "720p", actor: Actor = Depends(current_actor)):
    """Fallback single-stream transcode (?start= seconds) for players without HLS/MSE."""
    if profile not in transcode.PROFILES:
        raise HTTPException(404, "Unknown profile")
    item = _item_or_404(item_id, actor)
    meta = json.loads(item["meta"] or "{}")
    src = _source_for(item, meta)
    cmd = transcode.audio_cmd(src, start) if item["kind"] == "audio" else transcode.progressive_cmd(src, meta, start, profile)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def gen():
        try:
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    return StreamingResponse(gen(), media_type="video/mp4" if item["kind"] != "audio" else "audio/mp4",
                             headers={"Cache-Control": "no-store", "X-Stream-Start": str(start)})


@router.post("/items/{item_id}/optimize")
def optimize_item(item_id: int, profiles: str = "1080p,720p", actor: Actor = Depends(require_write)):
    """Pre-encode rungs into the cache so this item never waits on the transcoder."""
    from .. import jobs

    item = _item_or_404(item_id, actor)
    meta = json.loads(item["meta"] or "{}")
    src = _source_for(item, meta)
    wanted = [p for p in profiles.split(",") if p in transcode.PROFILES]
    return {"job_id": jobs.submit("optimize", str(item_id), lambda ctx: transcode.optimize(item, meta, src, wanted, ctx))}


# ---- captions -------------------------------------------------------------------------------

@router.get("/items/{item_id}/captions/live")
def live_captions(item_id: int, at: float = 0, actor: Actor = Depends(current_actor)):
    from .. import transcribe

    item = _item_or_404(item_id, actor)
    return transcribe.live(item, max(at, 0))


@router.get("/items/{item_id}/captions.vtt")
def captions_vtt(item_id: int, actor: Actor = Depends(current_actor)):
    from .. import transcribe

    _item_or_404(item_id, actor)
    return Response(transcribe.to_vtt(item_id), media_type="text/vtt")


@router.post("/items/{item_id}/transcribe")
def transcribe_item(item_id: int, actor: Actor = Depends(require_write)):
    from .. import transcribe

    item = _item_or_404(item_id, actor)
    if not transcribe.enabled():
        raise HTTPException(409, "Transcription isn't set up — see Settings → Transcription")
    return {"queued_chunks": transcribe.transcribe_all(item)}


@router.get("/items/{item_id}/subtitles/{index}.vtt")
def subtitle_stream(item_id: int, index: int, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    data = transcode.subtitle_vtt(safe_item_path(item), stream_index=index)
    return Response(data, media_type="text/vtt")


@router.get("/items/{item_id}/subtitles/file/{name}")
def subtitle_file(item_id: int, name: str, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    p = safe_item_path(item)
    side = (p.parent / name).resolve()
    if side.parent != p.parent.resolve() or not side.name.startswith(p.stem) or side.suffix.lower() not in kinds.SUBTITLE:
        raise HTTPException(404, "Not found")
    if side.suffix.lower() == ".vtt":
        return FileResponse(side, media_type="text/vtt")
    return Response(transcode.subtitle_vtt(p, sidecar=side), media_type="text/vtt")


class ProgressBody(BaseModel):
    position: float = Field(ge=0)
    duration: float | None = Field(default=None, ge=0)


@router.post("/items/{item_id}/progress")
def save_progress(item_id: int, body: ProgressBody, actor: Actor = Depends(current_actor)):
    item = _item_or_404(item_id, actor)
    dur = body.duration or item["duration"] or 0
    completed = 1 if dur and body.position >= dur * 0.92 else 0
    db.x("INSERT INTO progress(user_id, item_id, position, duration, completed, updated_at) VALUES(?,?,?,?,?,?) "
         "ON CONFLICT(user_id, item_id) DO UPDATE SET position=excluded.position, duration=excluded.duration, "
         "completed=excluded.completed, updated_at=excluded.updated_at",
         (actor.user_id, item_id, body.position, dur, completed, time.time()))
    return {"completed": bool(completed)}


@router.delete("/items/{item_id}/progress")
def clear_progress(item_id: int, actor: Actor = Depends(current_actor)):
    db.x("DELETE FROM progress WHERE user_id=? AND item_id=?", (actor.user_id, item_id))
    return {"ok": True}


# ---- collections -----------------------------------------------------------------------------

@router.get("/collections")
def collections(actor: Actor = Depends(current_actor)):
    return db.q("SELECT c.*, (SELECT COUNT(*) FROM collection_items ci WHERE ci.collection_id=c.id) AS count, "
                "(SELECT ci.item_id FROM collection_items ci JOIN items i ON i.id=ci.item_id WHERE ci.collection_id=c.id "
                " AND i.thumb_state='ok' ORDER BY ci.position LIMIT 1) AS cover_id FROM collections c ORDER BY c.created_at DESC")


@router.get("/collections/{cid}")
def collection(cid: int, actor: Actor = Depends(current_actor)):
    c = db.q1("SELECT * FROM collections WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "Not found")
    frag, ids = access.clause(access.library_ids(actor))
    rows = db.q(f"SELECT {catalog.ITEM_COLS} FROM collection_items ci JOIN items i ON i.id=ci.item_id "
                f"JOIN libraries l ON l.id=i.library_id WHERE ci.collection_id=? AND i.trashed_at IS NULL{frag} "
                f"ORDER BY ci.position", (cid, *ids))
    c["items"] = catalog.cards(rows)
    return c


class CollectionBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    item_ids: list[int] = []


@router.post("/collections")
def create_collection(body: CollectionBody, actor: Actor = Depends(require_write)):
    with db.tx():
        cid = db.x("INSERT INTO collections(name, description, created_by, created_at) VALUES(?,?,?,?)",
                   (body.name, body.description, actor.audit_name, time.time())).lastrowid
        for pos, iid in enumerate(body.item_ids):
            db.x("INSERT OR IGNORE INTO collection_items(collection_id, item_id, position) VALUES(?,?,?)", (cid, iid, pos))
    return {"id": cid}


@router.post("/collections/{cid}/items")
def add_to_collection(cid: int, body: IdsBody, actor: Actor = Depends(require_write)):
    start = db.q1("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM collection_items WHERE collection_id=?", (cid,))["p"]
    for n, iid in enumerate(body.ids):
        db.x("INSERT OR IGNORE INTO collection_items(collection_id, item_id, position) VALUES(?,?,?)", (cid, iid, start + n))
    return {"ok": True}


@router.delete("/collections/{cid}")
def delete_collection(cid: int, actor: Actor = Depends(require_write)):
    db.x("DELETE FROM collections WHERE id=?", (cid,))
    return {"ok": True}
