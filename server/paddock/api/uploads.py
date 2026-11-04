"""Resumable uploads — phone photo/video backup and drag-and-drop from the web.

Flow (each step is idempotent, so a flaky mobile connection just resumes):
  1. POST /api/uploads/check   {sha256: [...]}         → which files the server already has
  2. POST /api/uploads         {filename, size, sha256} → {upload_id, offset}
  3. PUT  /api/uploads/{id}?offset=N   raw bytes chunk  → {offset}
  4. POST /api/uploads/{id}/complete                     → verifies SHA-256, files it, catalogs it

Every refusal says why in plain words (the apps show it next to the file): no library to receive it, a library
this person can't add to, an unknown file type, a full disk, a read-only folder, a damaged transfer.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import access, db, kinds, projects, scanner
from ..actor import Actor
from ..config import settings
from .deps import require_write

router = APIRouter(prefix="/api/uploads")

MAX_CHUNK = 64 * 1024 * 1024
MAX_FILE = 200 * 1024**3

# Who is sending each upload right now. Only the newest request may write: an older one could be a second job on
# the same phone, or a phone that left Wi-Fi mid-chunk and still looks connected from here.
_senders: dict[str, object] = {}


def _target_library(actor: Actor, library_id: int | None, project_id: int | None = None, kind: str | None = None) -> dict:
    """Where an upload goes: a chosen library, the project's folder, or this person's backup library. Left to choose,
    it picks one that collects that kind of file: a song goes to Music rather than sitting unseen among the photos."""
    def writable(lib: dict | None) -> bool:
        return bool(lib) and access.can_write_library(actor, lib["id"])

    def fits(lib: dict) -> bool:
        return kind is None or kind in kinds.LIBRARY_ACCEPTS.get(lib["kind"], kinds.LIBRARY_ACCEPTS["mixed"])

    if project_id:
        p = db.q1("SELECT * FROM projects WHERE id=?", (project_id,))
        if not p or access.project_access(actor, p) != "write":
            raise HTTPException(403, "You can't add files to this project")
        lib = db.q1("SELECT * FROM libraries WHERE id=?", (p["upload_library_id"],)) if p.get("upload_library_id") else None
        if writable(lib):
            return lib
        usable = [lib for lib in db.q("SELECT * FROM libraries ORDER BY CASE kind WHEN 'mixed' THEN 0 WHEN 'home_videos' THEN 1 "
                                      "WHEN 'engineering' THEN 2 ELSE 3 END, id") if writable(lib)]
        for lib in usable:
            if fits(lib):
                return lib
        if usable:
            return usable[0]
        raise HTTPException(409, "There's no library this project can put files in. Ask an admin to add one you can write to.")
    if library_id:
        lib = db.q1("SELECT * FROM libraries WHERE id=?", (library_id,))
        if not lib or not access.can_read_library(actor, lib["id"]):
            raise HTTPException(404, "No such library")
        if not writable(lib):
            raise HTTPException(403, f"You can look at {lib['name']} but not add files to it")
        return lib
    mine = db.q1("SELECT upload_library_id FROM user_settings WHERE user_id=?", (actor.user_id,))
    if mine and mine["upload_library_id"]:
        lib = db.q1("SELECT * FROM libraries WHERE id=?", (mine["upload_library_id"],))
        if writable(lib) and fits(lib):
            return lib
    for lib in db.q("SELECT * FROM libraries WHERE kind='photos' ORDER BY id"):
        if json.loads(lib["options"] or "{}").get("backup_target") and writable(lib) and fits(lib):
            return lib
    home = {"audio": "music", "model3d": "engineering", "gcode": "engineering", "cad": "engineering", "pcb": "engineering",
            "document": "engineering", "red_clip": "red", "braw_clip": "red"}.get(kind or "", "photos")
    for lib in db.q("SELECT * FROM libraries ORDER BY CASE WHEN kind=? THEN 0 WHEN kind IN ('photos','home_videos','mixed') THEN 1 ELSE 2 END, id", (home,)):
        if writable(lib) and fits(lib):
            return lib
    if kind in (None, "photo", "video"):
        raise HTTPException(409, "There's no Photos library you can back up to yet. Ask an admin to create one "
                                 "(Manage server → Libraries) or to give you access to one.")
    raise HTTPException(409, f"There's no library you can add {kinds.KIND_NAMES.get(kind, kind)} files to yet. Ask an admin to create "
                             "one (Manage server → Libraries) or to give you access to one.")


def _disk_error(e: OSError, where: str) -> HTTPException:
    if e.errno == errno.ENOSPC:
        return HTTPException(507, f"The server's disk is full, so {where} can't take more files right now")
    if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return HTTPException(503, f"The server isn't allowed to write into {where} (the folder is read-only)")
    return HTTPException(503, f"The server couldn't save the file into {where} ({e.strerror or e})")


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w\-. ()]+", "_", name).strip(" .")
    return name[:180] or "upload"


class CheckBody(BaseModel):
    sha256: list[str] = Field(max_length=5000)


@router.post("/check")
def check(body: CheckBody, actor: Actor = Depends(require_write)):
    """What this person's phone needn't send. Their trash counts, so backup doesn't bring back what they deleted;
    a copy only someone else can see doesn't, or their own backup would quietly skip the file."""
    hashes = [h.lower() for h in body.sha256 if re.fullmatch(r"[0-9a-fA-F]{64}", h)]
    frag, ids = access.clause(access.library_ids(actor))
    have: set[str] = set()
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        marks = ",".join("?" * len(part))
        have |= {r["sha256"] for r in db.q(f"SELECT i.sha256 FROM items i WHERE i.sha256 IN ({marks}){frag}", (*part, *ids))}
    return {"have": sorted(have)}


class StartBody(BaseModel):
    filename: str = Field(min_length=1, max_length=400)
    size: int = Field(ge=0, le=MAX_FILE)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    mime: str | None = Field(default=None, max_length=120)
    taken_at: float | None = None
    library_id: int | None = None
    project_id: int | None = None
    subfolder: str | None = Field(default=None, max_length=200)


def _named_by_type(filename: str, mime: str | None) -> str:
    """Phones can name a picked photo "1000012345" or "1000012345.null"; its type still says what it is."""
    path = Path(filename.replace("\\", "/"))
    ext = None if kinds.classify(path.suffix) else kinds.ext_for_mime(mime)
    return path.stem + ext if ext else filename


def _your_copy(sha: str, actor: Actor) -> tuple[dict | None, bool]:
    """This person's copy of a file and whether it just came back from the trash. Someone else's private copy
    doesn't count, or they'd be told it's uploaded and never see it. Uploading what's in the trash means they want it."""
    for row in db.q("SELECT id, kind, library_id, trashed_at FROM items WHERE sha256=? ORDER BY trashed_at IS NOT NULL, id", (sha,)):
        if not row["trashed_at"] and access.can_read_library(actor, row["library_id"]):
            return row, False
        if row["trashed_at"] and access.can_write_library(actor, row["library_id"]):
            db.x("UPDATE items SET trashed_at=NULL WHERE id=?", (row["id"],))
            db.audit(actor.audit_name, "items.restored", ids=[row["id"]], count=1, by="upload")
            return row, True
    return None, False


@router.post("")
def start(body: StartBody, actor: Actor = Depends(require_write)):
    sha = body.sha256.lower()
    filename = _named_by_type(body.filename, body.mime)
    suffix = Path(filename).suffix
    if not kinds.classify(suffix):
        raise HTTPException(415, f"Paddock doesn't keep {suffix} files" if suffix
                            else "This file has no extension, so Paddock can't tell what kind of file it is")
    lib = _target_library(actor, body.library_id, body.project_id, kinds.classify(suffix))
    existing, restored = _your_copy(sha, actor)
    if existing:
        if body.project_id:     # already on the server: just put it in the project
            projects.add_items(body.project_id, [existing["id"]], projects.ROLE_FOR_KIND.get(existing["kind"], "reference"))
        return {"status": "duplicate", "item_id": existing["id"], "restored": restored}
    open_up = db.q1("SELECT * FROM uploads WHERE sha256=? AND user_id=? AND status='open'", (sha, actor.user_id))
    if open_up and open_up["size"] == body.size:
        return {"status": "resume", "upload_id": open_up["id"], "offset": open_up["received"], "chunk_size": 8 * 1024 * 1024}
    if open_up:     # an earlier try measured it wrong: resuming could never finish, so start clean
        _discard(open_up["id"])
    try:
        free = shutil.disk_usage(lib["path"]).free
    except OSError:
        raise HTTPException(503, f"The folder for {lib['name']} isn't reachable on the server right now")
    if free < body.size + 256 * 1024 * 1024:
        raise HTTPException(507, f"The server's disk is full: {lib['name']} has {free // (1024 * 1024)} MB free")
    uid = secrets.token_urlsafe(16)
    now = time.time()
    db.x("INSERT INTO uploads(id, user_id, device_id, filename, size, sha256, taken_at, received, status, created_at, updated_at) "
         "VALUES(?,?,?,?,?,?,?,0,'open',?,?)",
         (uid, actor.user_id, actor.device_id, _safe_name(filename), body.size, sha, body.taken_at, now, now))
    meta = {"library_id": lib["id"], "subfolder": body.subfolder, "project_id": body.project_id}
    (settings.uploads_tmp_dir / f"{uid}.json").write_text(json.dumps(meta))
    return {"status": "new", "upload_id": uid, "offset": 0, "chunk_size": 8 * 1024 * 1024}


def _discard(uid: str) -> None:
    """Give up on a stored attempt, so the next try starts clean instead of resuming something that can't finish."""
    (settings.uploads_tmp_dir / f"{uid}.part").unlink(missing_ok=True)
    (settings.uploads_tmp_dir / f"{uid}.json").unlink(missing_ok=True)
    db.x("UPDATE uploads SET status='failed', received=0, updated_at=? WHERE id=?", (time.time(), uid))


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
    # No awaiting until this request is registered as the sender, so two can't both believe they're next.
    up = _upload(uid, actor)
    if up["status"] != "open":
        raise HTTPException(409, "Upload is already finished")
    if offset != up["received"]:
        raise HTTPException(409, f"Expected offset {up['received']}", headers={"Upload-Offset": str(up["received"])})
    tmp = settings.uploads_tmp_dir / f"{uid}.part"
    written = 0
    me = object()
    _senders[uid] = me

    def still_mine() -> None:
        if _senders.get(uid) is not me:
            now = _upload(uid, actor)["received"]
            raise HTTPException(409, f"Expected offset {now}", headers={"Upload-Offset": str(now)})

    try:
        with tmp.open("ab") as f:
            if f.tell() != offset:  # crash between write and DB update: trust the DB, truncate the file
                f.truncate(offset)
                f.seek(offset)
            async for chunk in request.stream():
                still_mine()
                written += len(chunk)
                if written > MAX_CHUNK or offset + written > up["size"]:
                    f.truncate(offset)
                    raise HTTPException(413, "Chunk too large")
                f.write(chunk)
                f.flush()   # nothing left buffered to land on top of a newer sender's data
        still_mine()
    except OSError as e:
        raise _disk_error(e, "the server's upload area")
    finally:
        if _senders.get(uid) is me:
            del _senders[uid]
    new_offset = offset + written
    db.x("UPDATE uploads SET received=?, updated_at=? WHERE id=?", (new_offset, time.time(), uid))
    return {"offset": new_offset}


def _finish(uid: str, actor: Actor) -> dict:
    up = _upload(uid, actor)
    if up["status"] == "done":
        return {"status": "done", "item_id": up["item_id"]}
    tmp = settings.uploads_tmp_dir / f"{uid}.part"
    if not tmp.exists() or tmp.stat().st_size != up["size"]:
        if up["received"] >= up["size"]:    # every byte counted, yet the file is wrong: it could never finish
            _discard(uid)
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
    project_id = meta.get("project_id")
    lib = _target_library(actor, meta.get("library_id"), project_id)
    root = Path(lib["path"])
    when = datetime.fromtimestamp(up["taken_at"] or time.time())
    who = "Uploads"
    if up["device_id"]:
        dev = db.q1("SELECT name FROM devices WHERE id=?", (up["device_id"],))
        who = f"Phone Backups/{_safe_name(dev['name']) if dev else 'Phone'}"
        mine = db.q1("SELECT upload_folder FROM user_settings WHERE user_id=?", (actor.user_id,))
        if mine and mine["upload_folder"]:     # this person's own folder, e.g. "Family/Aiden"
            who = "/".join(_safe_name(p) for p in mine["upload_folder"].replace("\\", "/").split("/") if p.strip(" ."))
    folder = root / who / f"{when:%Y}" / f"{when:%Y-%m}"
    if project_id:
        pname = (db.q1("SELECT name FROM projects WHERE id=?", (project_id,)) or {}).get("name") or f"Project {project_id}"
        folder = root / "Projects" / _safe_name(pname)
    elif meta.get("subfolder"):
        folder = root / "Uploads" / _safe_name(meta["subfolder"])
    if not scanner.within(folder, root):
        raise HTTPException(400, "Bad destination")
    dest = folder / up["filename"]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        n = 1
        while dest.exists():
            dest = folder / f"{Path(up['filename']).stem} ({n}){Path(up['filename']).suffix}"
            n += 1
        _move(tmp, dest)  # rename when on the same dataset, copy+delete across datasets
    except OSError as e:
        raise _disk_error(e, lib["name"])
    if up["taken_at"]:
        os.utime(dest, (up["taken_at"], up["taken_at"]))
    meta_p.unlink(missing_ok=True)
    item_id = scanner.register_file(lib["id"], dest)
    if item_id:
        db.x("UPDATE items SET sha256=? WHERE id=?", (up["sha256"], item_id))
    db.x("UPDATE uploads SET status='done', item_id=?, updated_at=? WHERE id=?", (item_id, time.time(), uid))
    if item_id and project_id:
        kind = (db.q1("SELECT kind FROM items WHERE id=?", (item_id,)) or {}).get("kind")
        projects.add_items(project_id, [item_id], projects.ROLE_FOR_KIND.get(kind, "reference"))
    db.audit(actor.audit_name, "upload.complete", file=dest.relative_to(root).as_posix(), item=item_id)
    from .. import ai

    if item_id and ai.config().get("autotag_on_upload"):
        from .. import jobs

        jobs.submit("autotag", f"item:{item_id}", lambda ctx: ai.autotag([item_id], ctx))
    return {"status": "done", "item_id": item_id, "path": dest.relative_to(root).as_posix()}


def _move(src: Path, dst: Path) -> None:
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
