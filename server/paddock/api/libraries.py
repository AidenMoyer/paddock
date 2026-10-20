"""Libraries: pick NAS folders, scan them, browse the allowed directory tree."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, kinds, scanner
from ..actor import Actor
from ..config import settings
from .deps import current_actor, require_admin

router = APIRouter(prefix="/api")


@router.get("/libraries")
def list_libraries(actor: Actor = Depends(current_actor)):
    rows = db.q("SELECT * FROM libraries ORDER BY name")
    for r in rows:
        r["options"] = json.loads(r["options"] or "{}")
        r["kind_label"] = kinds.LIBRARY_KINDS.get(r["kind"], r["kind"])
        if not actor.is_admin:
            r.pop("path", None)
    return rows


@router.get("/library-kinds")
def library_kinds():
    return [{"kind": k, "label": v} for k, v in kinds.LIBRARY_KINDS.items()]


class LibraryBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str
    path: str = Field(max_length=1000)
    options: dict = {}


def _check_path(path: str, exclude_id: int | None = None) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(400, "Use a full path")
    p = p.resolve()
    if not scanner.allowed_root(p):
        raise HTTPException(400, f"Folder must be inside: {', '.join(str(r) for r in settings.media_roots)}")
    if not p.is_dir():
        raise HTTPException(400, "That folder doesn't exist")
    for lib in db.q("SELECT id, path, name FROM libraries"):
        if lib["id"] == exclude_id:
            continue
        other = Path(lib["path"])
        if scanner.within(p, other) or scanner.within(other, p):
            raise HTTPException(409, f"Overlaps the library '{lib['name']}'")
    return p


@router.post("/libraries")
def create_library(body: LibraryBody, actor: Actor = Depends(require_admin)):
    if body.kind not in kinds.LIBRARY_KINDS:
        raise HTTPException(400, "Unknown library type")
    p = _check_path(body.path)
    opts = dict(body.options)
    if body.kind == "engineering":
        opts.setdefault("auto_file", True)
        opts.setdefault("inbox", "_Inbox")
        try:
            (p / opts["inbox"]).mkdir(exist_ok=True)
        except OSError:
            opts["auto_file"] = False  # read-only dataset — filing disabled
    if body.kind == "photos":
        opts.setdefault("backup_target", not db.q1("SELECT 1 FROM libraries WHERE kind='photos'"))
    lid = db.x("INSERT INTO libraries(name, kind, path, options, created_at) VALUES(?,?,?,?,?)",
               (body.name.strip(), body.kind, str(p), json.dumps(opts), time.time())).lastrowid
    db.audit(actor.audit_name, "library.created", library=lid, path=str(p), kind=body.kind)
    job = scanner.queue_scan(lid)
    return {"id": lid, "job_id": job}


class LibraryPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    options: dict | None = None


@router.patch("/libraries/{lib_id}")
def update_library(lib_id: int, body: LibraryPatch, actor: Actor = Depends(require_admin)):
    lib = db.q1("SELECT * FROM libraries WHERE id=?", (lib_id,))
    if not lib:
        raise HTTPException(404, "No such library")
    if body.name:
        db.x("UPDATE libraries SET name=? WHERE id=?", (body.name.strip(), lib_id))
    if body.options is not None:
        opts = json.loads(lib["options"] or "{}")
        opts.update(body.options)
        if opts.get("backup_target"):
            # only one backup target at a time
            for other in db.q("SELECT id, options FROM libraries WHERE id!=?", (lib_id,)):
                o = json.loads(other["options"] or "{}")
                if o.pop("backup_target", None):
                    db.x("UPDATE libraries SET options=? WHERE id=?", (json.dumps(o), other["id"]))
        db.x("UPDATE libraries SET options=? WHERE id=?", (json.dumps(opts), lib_id))
    return {"ok": True}


@router.delete("/libraries/{lib_id}")
def delete_library(lib_id: int, actor: Actor = Depends(require_admin)):
    """Removes the library from Paddock's catalog. Files on disk are untouched."""
    with db.tx():
        for r in db.q("SELECT id FROM items WHERE library_id=?", (lib_id,)):
            db.fts_remove(r["id"])
        db.x("DELETE FROM libraries WHERE id=?", (lib_id,))
    db.audit(actor.audit_name, "library.removed", library=lib_id)
    return {"ok": True}


@router.post("/libraries/{lib_id}/scan")
def scan(lib_id: int, full: bool = False, actor: Actor = Depends(current_actor)):
    if not actor.can("write"):
        raise HTTPException(403, "Read-only")
    if not db.q1("SELECT 1 FROM libraries WHERE id=?", (lib_id,)):
        raise HTTPException(404, "No such library")
    return {"job_id": scanner.queue_scan(lib_id, full=full)}


WINDOWS_SYSTEM_DIRS = {"system volume information", "recovery", "config.msi", "perflogs", "documents and settings",
                       "windows", "programdata", "msocache", "boot", "onedrivetemp"}


@router.get("/fs/browse")
def browse(path: str | None = None, actor: Actor = Depends(require_admin)):
    """Directory picker. Only shows folders inside the configured media roots."""
    roots = []
    for r in settings.media_roots:
        try:
            if r.is_dir():
                roots.append(r)
        except OSError:
            continue  # unreachable network drive
    if not path:
        return {"path": None, "parent": None, "roots": [str(r) for r in roots],
                "dirs": [{"name": str(r), "path": str(r)} for r in roots]}
    p = Path(path).resolve()
    if not scanner.allowed_root(p) or not p.is_dir():
        raise HTTPException(400, "Not an allowed folder")
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            name = child.name
            if name.startswith((".", "$")) or name in scanner.SKIP_DIRS or name.lower() in WINDOWS_SYSTEM_DIRS:
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "Paddock can't read that folder (check dataset permissions)")
    parent = str(p.parent) if any(scanner.within(p.parent, r) for r in roots) and p not in roots else None
    counts: dict[str, int] = {}
    try:
        for f in list(p.iterdir())[:5000]:
            if f.is_file():
                k = kinds.classify(f.suffix) or "other"
                counts[k] = counts.get(k, 0) + 1
    except OSError:
        pass
    return {"path": str(p), "parent": parent, "roots": [str(r) for r in roots], "dirs": dirs[:2000], "files": counts}
