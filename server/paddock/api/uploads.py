"""Resumable uploads — phone photo/video backup and drag-and-drop from the web.

Flow (each step is idempotent, so a flaky mobile connection just resumes):
  1. POST /api/uploads/check   {sha256: [...]}         → which files the server already has
  2. POST /api/uploads         {filename, size, sha256} → {upload_id, offset}
  3. PUT  /api/uploads/{id}?offset=N   raw bytes chunk  → {offset}
  4. POST /api/uploads/{id}/complete                     → verifies SHA-256, files it, catalogs it
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, kinds, scanner
from ..actor import Actor
from ..config import settings
from .deps import require_write

router = APIRouter(prefix="/api/uploads")

MAX_CHUNK = 64 * 1024 * 1024
MAX_FILE = 200 * 1024**3


def _target_library(library_id: int | None, kind: str | None) -> dict:
    if library_id:
        lib = db.q1("SELECT * FROM libraries WHERE id=?", (library_id,))
        if not lib:
            raise HTTPException(404, "No such library")
        return lib
    for lib in db.q("SELECT * FROM libraries WHERE kind='photos' ORDER BY id"):
        if json.loads(lib["options"] or "{}").get("backup_target"):
            return lib
    lib = db.q1("SELECT * FROM libraries WHERE kind IN ('photos','home_videos','mixed') ORDER BY id")
    if not lib:
        raise HTTPException(409, "Create a Photos library first (Settings → Libraries) to receive backups")
    return lib


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w\-. ()]+", "_", name).strip(" .")
    return name[:180] or "upload"


class CheckBody(BaseModel):
    sha256: list[str] = Field(max_length=5000)


@router.post("/check")
def check(body: CheckBody, actor: Actor = Depends(require_write)):
    hashes = [h.lower() for h in body.sha256 if re.fullmatch(r"[0-9a-fA-F]{64}", h)]
    have: set[str] = set()
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        marks = ",".join("?" * len(part))
        have |= {r["sha256"] for r in db.q(f"SELECT sha256 FROM items WHERE sha256 IN ({marks})", part)}
    return {"have": sorted(have)}


class StartBody(BaseModel):
    filename: str = Field(min_length=1, max_length=400)
    size: int = Field(ge=0, le=MAX_FILE)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    taken_at: float | None = None
    library_id: int | None = None
    subfolder: str | None = Field(default=None, max_length=200)


@router.post("")
def start(body: StartBody, actor: Actor = Depends(require_write)):
    sha = body.sha256.lower()
    if not kinds.classify(Path(body.filename).suffix):
        raise HTTPException(415, "Paddock doesn't know this file type")
    existing = db.q1("SELECT id FROM items WHERE sha256=?", (sha,))
    if existing:
        return {"status": "duplicate", "item_id": existing["id"]}
    open_up = db.q1("SELECT * FROM uploads WHERE sha256=? AND user_id=? AND status='open'", (sha, actor.user_id))
    if open_up:
        return {"status": "resume", "upload_id": open_up["id"], "offset": open_up["received"], "chunk_size": 8 * 1024 * 1024}
    lib = _target_library(body.library_id, None)
    uid = secrets.token_urlsafe(16)
    now = time.time()
    db.x("INSERT INTO uploads(id, user_id, device_id, filename, size, sha256, taken_at, received, status, created_at, updated_at) "
         "VALUES(?,?,?,?,?,?,?,0,'open',?,?)",
         (uid, actor.user_id, actor.device_id, _safe_name(body.filename), body.size, sha, body.taken_at, now, now))
    meta = {"library_id": lib["id"], "subfolder": body.subfolder}
    (settings.uploads_tmp_dir / f"{uid}.json").write_text(json.dumps(meta))
    return {"status": "new", "upload_id": uid, "offset": 0, "chunk_size": 8 * 1024 * 1024}


def _upload(uid: str, actor: Actor) -> dict:
    up = db.q1("SELECT * FROM uploads WHERE id=? AND user_id=?", (uid, actor.user_id))
    if not up:
        raise HTTPException(404, "No such upload")
    return up


@router.get("/{uid}")
def status(uid: str, actor: Actor = Depends(require_write)):
    up = _upload(uid, actor)
    return {"offset": up["received"], "size": up["size"], "status": up["status"], "item_id": up["item_id"]}


@router.put("/{uid}")
async def put_chunk(uid: str, offset: int, request: Request, actor: Actor = Depends(require_write)):
    up = await run_in_threadpool(_upload, uid, actor)
    if up["status"] != "open":
        raise HTTPException(409, "Upload is already finished")
    if offset != up["received"]:
        raise HTTPException(409, f"Expected offset {up['received']}", headers={"Upload-Offset": str(up["received"])})
    tmp = settings.uploads_tmp_dir / f"{uid}.part"
    written = 0
    with tmp.open("ab") as f:
        if f.tell() != offset:  # crash between write and DB update: trust the DB, truncate the file
            f.truncate(offset)
            f.seek(offset)
        async for chunk in request.stream():
            written += len(chunk)
            if written > MAX_CHUNK or offset + written > up["size"]:
                f.truncate(offset)
                raise HTTPException(413, "Chunk too large")
            f.write(chunk)
    new_offset = offset + written
    db.x("UPDATE uploads SET received=?, updated_at=? WHERE id=?", (new_offset, time.time(), uid))
    return {"offset": new_offset}


def _finish(uid: str, actor: Actor) -> dict:
    up = _upload(uid, actor)
    if up["status"] == "done":
        return {"status": "done", "item_id": up["item_id"]}
    tmp = settings.uploads_tmp_dir / f"{uid}.part"
    if not tmp.exists() or tmp.stat().st_size != up["size"]:
        raise HTTPException(409, "Upload is incomplete")
    h = hashlib.sha256()
    with tmp.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    if h.hexdigest() != up["sha256"]:
        tmp.unlink(missing_ok=True)
        db.x("UPDATE uploads SET status='failed', received=0 WHERE id=?", (uid,))
        raise HTTPException(422, "Checksum mismatch — the file was corrupted in transit; it will be re-sent")
    meta_p = settings.uploads_tmp_dir / f"{uid}.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    lib = _target_library(meta.get("library_id"), None)
    root = Path(lib["path"])
    when = datetime.fromtimestamp(up["taken_at"] or time.time())
    who = "Uploads"
    if up["device_id"]:
        dev = db.q1("SELECT name FROM devices WHERE id=?", (up["device_id"],))
        who = f"Phone Backups/{_safe_name(dev['name']) if dev else 'Phone'}"
    folder = root / who / f"{when:%Y}" / f"{when:%Y-%m}"
    if meta.get("subfolder"):
        folder = root / "Uploads" / _safe_name(meta["subfolder"])
    if not scanner.within(folder, root):
        raise HTTPException(400, "Bad destination")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / up["filename"]
    n = 1
    while dest.exists():
        dest = folder / f"{Path(up['filename']).stem} ({n}){Path(up['filename']).suffix}"
        n += 1
    _move(tmp, dest)  # rename when on the same dataset, copy+delete across datasets
    if up["taken_at"]:
        os.utime(dest, (up["taken_at"], up["taken_at"]))
    meta_p.unlink(missing_ok=True)
    item_id = scanner.register_file(lib["id"], dest)
    if item_id:
        db.x("UPDATE items SET sha256=? WHERE id=?", (up["sha256"], item_id))
    db.x("UPDATE uploads SET status='done', item_id=?, updated_at=? WHERE id=?", (item_id, time.time(), uid))
    db.audit(actor.audit_name, "upload.complete", file=dest.relative_to(root).as_posix(), item=item_id)
    from .. import ai

    if item_id and ai.config().get("autotag_on_upload"):
        from .. import jobs

        jobs.submit("autotag", f"item:{item_id}", lambda ctx: ai.autotag([item_id], ctx))
    return {"status": "done", "item_id": item_id, "path": dest.relative_to(root).as_posix()}


def _move(src: Path, dst: Path) -> None:
    import shutil

    shutil.move(str(src), str(dst))


@router.post("/{uid}/complete")
async def complete(uid: str, actor: Actor = Depends(require_write)):
    return await run_in_threadpool(_finish, uid, actor)


@router.delete("/{uid}")
def cancel(uid: str, actor: Actor = Depends(require_write)):
    _upload(uid, actor)
    (settings.uploads_tmp_dir / f"{uid}.part").unlink(missing_ok=True)
    (settings.uploads_tmp_dir / f"{uid}.json").unlink(missing_ok=True)
    db.x("DELETE FROM uploads WHERE id=?", (uid,))
    return {"ok": True}
