"""Library scanning: walk → classify → probe → catalog → thumbnail."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from . import db, engineering, jobs, kinds, probe, red, thumbs
from .config import settings

log = logging.getLogger("paddock.scanner")

SKIP_DIRS = {"@eaDir", "#recycle", ".Trash", ".Trashes", "$RECYCLE.BIN", "System Volume Information",
             ".zfs", "lost+found", ".paddock", "_duplicates", ".thumbnails", "Proxies"}


# ---- path safety -----------------------------------------------------------------

def within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def allowed_root(path: Path) -> bool:
    return any(within(path, r) for r in settings.media_roots)


def safe_item_path(item: dict) -> Path:
    """Resolve an item's file and refuse anything that escaped its library (symlinks, renames)."""
    lib = db.q1("SELECT path FROM libraries WHERE id=?", (item["library_id"],))
    p = Path(item["path"])
    if not lib or not within(p, Path(lib["path"])) or not allowed_root(p):
        raise PermissionError("item path is outside its library")
    return p


# ---- scanning --------------------------------------------------------------------

def queue_scan(library_id: int, full: bool = False) -> int:
    return jobs.submit("scan", str(library_id), lambda ctx: scan_library(library_id, ctx, full=full))


def file_all_inboxes() -> None:
    for lib in db.q("SELECT * FROM libraries WHERE kind IN ('engineering','mixed')"):
        opts = json.loads(lib["options"] or "{}")
        if not opts.get("auto_file", lib["kind"] == "engineering"):
            continue
        moves = engineering.file_inbox(Path(lib["path"]), opts.get("inbox", "_Inbox"))
        if moves:
            db.audit("system", "inbox.filed", library=lib["id"], moves=moves[:50], count=len(moves))
            queue_scan(lib["id"])


def _walk(root: Path, inbox: str | None):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                       and not (inbox and Path(dirpath) == root and d == inbox)]
        for fn in filenames:
            if fn.startswith(".") or fn.startswith("._"):
                continue
            yield Path(dirpath) / fn


def scan_library(library_id: int, ctx: jobs.JobContext | None = None, full: bool = False) -> str:
    lib = db.q1("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not lib:
        return "library gone"
    root = Path(lib["path"])
    if not root.is_dir():
        db.x("UPDATE libraries SET scan_state='missing' WHERE id=?", (library_id,))
        return "library folder not found"
    opts = json.loads(lib["options"] or "{}")
    accepts = kinds.LIBRARY_ACCEPTS.get(lib["kind"], kinds.LIBRARY_ACCEPTS["mixed"])
    inbox = opts.get("inbox", "_Inbox") if lib["kind"] in ("engineering", "mixed") else None
    db.x("UPDATE libraries SET scan_state='scanning' WHERE id=?", (library_id,))

    existing = {r["path"]: r for r in db.q("SELECT id, path, size, mtime FROM items WHERE library_id=?", (library_id,))}
    seen: set[str] = set()
    files = list(_walk(root, inbox))
    added = updated = 0
    skipped: dict[str, int] = {}
    for n, path in enumerate(files):
        if ctx and n % 25 == 0:
            ctx.progress(0.7 * n / max(len(files), 1), f"Reading {lib['name']}: {n}/{len(files)} files")
        kind = kinds.classify(path.suffix)
        if kind is None:
            continue
        if kind not in accepts:
            if kind != "document":  # stray notes/readmes aren't worth a warning
                skipped[kind] = skipped.get(kind, 0) + 1
            continue
        if kind == "red_clip" and red.is_secondary_span(path):
            continue
        if kind == "video" and path.parent.suffix.upper() == ".RDC":
            continue  # camera/REDline proxy — attached to its clip, not a separate item
        if not within(path, root):  # symlink escaping the library
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        seen.add(key)
        old = existing.get(key)
        if old and not full and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1:
            continue
        try:
            item_id = upsert_item(lib, root, path, kind, st, old["id"] if old else None)
        except Exception:
            log.exception("failed to catalog %s", path)
            continue
        if old:
            updated += 1
        else:
            added += 1
            if lib["kind"] in ("engineering", "mixed") and kind in ("model3d", "gcode", "cad", "pcb"):
                _maybe_link_project(item_id)

    removed = 0
    with db.tx():
        for p, row in existing.items():
            if p not in seen:
                db.fts_remove(row["id"])
                db.x("DELETE FROM items WHERE id=?", (row["id"],))
                removed += 1
    count = db.q1("SELECT COUNT(*) c FROM items WHERE library_id=?", (library_id,))["c"]
    db.x("UPDATE libraries SET scan_state='thumbnails', last_scan=?, item_count=? WHERE id=?",
         (time.time(), count, library_id))

    pending = db.q("SELECT * FROM items WHERE library_id=? AND thumb_state='pending'", (library_id,))
    for n, item in enumerate(pending):
        if ctx and n % 5 == 0:
            ctx.progress(0.7 + 0.3 * n / max(len(pending), 1), f"Thumbnails: {n}/{len(pending)}")
        state = thumbs.generate(item)
        db.x("UPDATE items SET thumb_state=? WHERE id=?", (state, item["id"]))
    db.x("UPDATE libraries SET scan_state='idle' WHERE id=?", (library_id,))
    msg = f"{added} added, {updated} updated, {removed} removed"
    if skipped:
        names = {"audio": "audio", "red_clip": "RED", "braw_clip": "BRAW", "photo": "photo", "video": "video",
                 "model3d": "3D model", "gcode": "G-code", "cad": "CAD", "pcb": "PCB", "document": "document"}
        msg += " · skipped " + ", ".join(f"{v} {names.get(k, k)} files" for k, v in skipped.items()) +                f" (a {kinds.LIBRARY_KINDS.get(lib['kind'], lib['kind'])} library doesn't show those)"
    db.audit("system", "library.scanned", library=library_id, result=msg)
    return msg


def _maybe_link_project(item_id: int) -> None:
    """Engineering files filed under <Project>/... join a Paddock project of the same name, if one exists."""
    item = db.q1("SELECT rel_path FROM items WHERE id=?", (item_id,))
    top = Path(item["rel_path"]).parts[0] if item else None
    if not top:
        return
    proj = db.q1("SELECT id FROM projects WHERE name=? COLLATE NOCASE", (top,))
    if proj:
        db.x("INSERT OR IGNORE INTO project_items(project_id, item_id, role, added_at) VALUES(?,?,?,?)",
             (proj["id"], item_id, "cad", time.time()))


def _resolution_tag(w: int | None, h: int | None) -> str | None:
    if not w or not h:
        return None
    long_side = max(w, h)
    if long_side >= 7680:
        return "8K"
    if long_side >= 3840:
        return "4K"
    if long_side >= 2560:
        return "1440p"
    if long_side >= 1920:
        return "1080p"
    if long_side >= 1280:
        return "720p"
    return "SD"


def upsert_item(lib: dict, root: Path, path: Path, kind: str, st: os.stat_result, item_id: int | None) -> int:
    rel = path.relative_to(root).as_posix()
    meta: dict = {}
    title = probe.clean_title(path.stem)
    taken = None
    duration = width = height = lat = lon = None
    group_key = sort_key = None
    auto_tags: set[str] = set()

    if kind in ("video", "braw_clip"):
        info = probe.av_info(path) if kind == "video" else red.braw_meta(path)
        meta.update({k: v for k, v in info.items() if v is not None})
        duration, width, height = info.get("duration"), info.get("width"), info.get("height")
        lat, lon = info.get("lat"), info.get("lon")
        taken = info.get("created")
        if info.get("hdr"):
            auto_tags.add("HDR")
        if kind == "braw_clip":
            auto_tags.add("BRAW")
    elif kind == "audio":
        info = probe.audio_tags(path)
        meta.update(info)
        duration = info.get("duration")
        title = info.get("title") or title
        artist = info.get("albumartist") or info.get("artist") or "Unknown artist"
        album = info.get("album") or path.parent.name
        meta.setdefault("artist", artist)
        meta.setdefault("album", album)
        group_key = f"album:{artist}:{album}".lower()
        sort_key = f"{info.get('disc') or 1:02d}-{info.get('track') or 0:03d}-{title.lower()}"
        if info.get("genre"):
            auto_tags.add(info["genre"])
    elif kind == "photo":
        info = probe.photo_info(path)
        meta.update(info)
        width, height = info.get("width"), info.get("height")
        lat, lon = info.get("lat"), info.get("lon")
        taken = info.get("created")
        if path.suffix.lower() in kinds.RAW_PHOTO:
            auto_tags.add("RAW")
        if "screenshot" in path.name.lower():
            auto_tags.add("Screenshot")
    elif kind == "red_clip":
        info = red.describe_clip(path)
        meta.update(info)
        title = info.get("clip_name") or title
        duration = info.get("duration")
        width, height = info.get("width"), info.get("height")
        auto_tags.add("R3D")
        if info.get("camera_model"):
            auto_tags.add(info["camera_model"])
    elif kind in ("model3d", "gcode", "cad", "pcb", "document"):
        info, _ = engineering.describe(path) if kind != "document" else ({}, None)
        meta.update(info)
        if info.get("title"):
            meta["model_title"] = info["title"]
        for k in ("format", "category", "filament_type", "slicer"):
            if info.get(k):
                auto_tags.add(str(info[k]).split(" ")[0] if k == "slicer" else str(info[k]))

    if meta.get("model"):
        auto_tags.add(str(meta["model"]))
    res = _resolution_tag(width, height)
    if res and kind in ("video", "red_clip", "braw_clip"):
        auto_tags.add(res)

    # library-specific naming / grouping
    if lib["kind"] == "movies" and kind == "video":
        mv = probe.parse_movie(path)
        title = mv["title"]
        meta["year"] = mv["year"]
        group_key = f"movie:{title}:{mv['year']}".lower()
        sort_key = title.lower()
    elif lib["kind"] == "shows" and kind == "video":
        ep = probe.parse_episode(path, root)
        meta.update({k: v for k, v in ep.items() if v is not None})
        s, e = ep.get("season"), ep.get("episode")
        title = ep.get("episode_title") or (f"Episode {e}" if e else title)
        group_key = f"show:{ep['show']}".lower()
        sort_key = f"S{s or 0:02d}E{e or 0:03d}"
    if taken is None:
        taken = st.st_mtime
    if kind in ("red_clip", "braw_clip"):
        taken = red.shoot_date(meta, st.st_mtime)
        day = datetime.fromtimestamp(taken).strftime("%Y-%m-%d")
        group_key = f"shoot:{day}"
        sort_key = f"{meta.get('camera', 'Z')}{meta.get('reel', 0):03d}C{meta.get('clip', 0):03d}"
    elif kind in ("model3d", "gcode", "cad", "pcb"):
        group_key = engineering.family_key(rel)
        sort_key = f"{st.st_mtime:.0f}"
    elif lib["kind"] in ("photos", "home_videos") or (lib["kind"] == "mixed" and kind in ("photo", "video")):
        group_key = group_key or f"day:{datetime.fromtimestamp(taken).strftime('%Y-%m-%d')}"
        sort_key = sort_key or f"{taken:.0f}"
    if group_key is None:
        group_key = f"folder:{Path(rel).parent.as_posix()}"

    now = time.time()
    values = dict(
        library_id=lib["id"], path=str(path), rel_path=rel, kind=kind, ext=path.suffix.lower(),
        size=meta.get("total_size", st.st_size), mtime=st.st_mtime, title=title[:300], taken_at=taken,
        duration=duration, width=width, height=height, lat=lat, lon=lon, group_key=group_key,
        sort_key=sort_key, meta=json.dumps(meta, default=str),
    )
    with db.tx():
        if item_id:
            sets = ", ".join(f"{k}=?" for k in values)
            db.x(f"UPDATE items SET {sets}, thumb_state='pending' WHERE id=?", (*values.values(), item_id))
        else:
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            item_id = db.x(f"INSERT INTO items({cols}, added_at) VALUES({marks}, ?)", (*values.values(), now)).lastrowid
        db.x("DELETE FROM tags WHERE item_id=? AND source='auto'", (item_id,))
        for t in auto_tags:
            if t and len(t) < 60:
                db.x("INSERT OR IGNORE INTO tags(item_id, tag, source) VALUES(?,?, 'auto')", (item_id, t))
        db.fts_index(item_id)
    return item_id


def register_file(library_id: int, path: Path) -> int | None:
    """Catalog a single new file immediately (uploads), then thumbnail it."""
    lib = db.q1("SELECT * FROM libraries WHERE id=?", (library_id,))
    if not lib:
        return None
    root = Path(lib["path"])
    kind = kinds.classify(path.suffix)
    if kind is None:
        return None
    existing = db.q1("SELECT id FROM items WHERE path=?", (str(path),))
    item_id = upsert_item(lib, root, path, kind, path.stat(), existing["id"] if existing else None)
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    db.x("UPDATE items SET thumb_state=? WHERE id=?", (thumbs.generate(item), item_id))
    db.x("UPDATE libraries SET item_count=(SELECT COUNT(*) FROM items WHERE library_id=?) WHERE id=?",
         (library_id, library_id))
    return item_id
