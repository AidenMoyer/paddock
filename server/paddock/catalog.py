"""Read-side queries over the catalog, shared by the REST API, MCP tools and the assistant."""

from __future__ import annotations

import json
import time
from datetime import datetime

from . import db

ITEM_COLS = ("i.id, i.library_id, i.kind, i.ext, i.title, i.rel_path, i.size, i.taken_at, i.added_at, i.duration, "
             "i.width, i.height, i.lat, i.lon, i.group_key, i.sort_key, i.thumb_state, i.favorite, i.meta, "
             "l.name AS library_name, l.kind AS library_kind")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def card(row: dict, user_id: int | None = None, tags: list[str] | None = None) -> dict:
    """Compact item representation for grids, feeds and AI tool results."""
    meta = json.loads(row.get("meta") or "{}")
    out = {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "ext": row["ext"],
        "library_id": row["library_id"],
        "library": row.get("library_name"),
        "library_kind": row.get("library_kind"),
        "path": row["rel_path"],
        "size": row["size"],
        "taken_at": row["taken_at"],
        "date": _iso(row["taken_at"]),
        "added_at": row["added_at"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "has_thumb": row["thumb_state"] == "ok",
        "favorite": bool(row["favorite"]),
        "group": row["group_key"],
    }
    if row.get("lat") is not None:
        out["lat"], out["lon"] = row["lat"], row["lon"]
    # a few kind-specific facts that make cards informative
    for k in ("artist", "album", "show", "season", "episode", "year", "camera", "reel", "clip", "camera_model",
              "format", "category", "revision", "print_time", "filament_g", "filament_type", "size_x", "size_y",
              "size_z", "vcodec", "fps", "model", "proxy"):
        if meta.get(k) not in (None, ""):
            out[k] = meta[k] if k != "proxy" else True
    if tags is not None:
        out["tags"] = tags
    if user_id is not None and "position" in row:
        out["progress"] = {"position": row["position"], "completed": bool(row["completed"])}
    return out


def tags_for(ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, list[str]] = {i: [] for i in ids}
    for r in db.q(f"SELECT item_id, tag FROM tags WHERE item_id IN ({marks}) ORDER BY tag", ids):
        out[r["item_id"]].append(r["tag"])
    return out


def cards(rows: list[dict], user_id: int | None = None, with_tags: bool = True) -> list[dict]:
    t = tags_for([r["id"] for r in rows]) if with_tags else {}
    return [card(r, user_id, t.get(r["id"]) if with_tags else None) for r in rows]


def get_item(item_id: int, user_id: int | None = None, include_trashed: bool = False) -> dict | None:
    row = db.q1(f"SELECT {ITEM_COLS}, i.path AS abs_path, i.mtime, i.trashed_at FROM items i "
                f"JOIN libraries l ON l.id=i.library_id WHERE i.id=?", (item_id,))
    if not row or (row["trashed_at"] and not include_trashed):
        return None
    out = card(row, tags=[r["tag"] for r in db.q("SELECT tag FROM tags WHERE item_id=? ORDER BY tag", (item_id,))])
    out["meta"] = json.loads(row["meta"] or "{}")
    out["projects"] = db.q("SELECT p.id, p.name, pi.role FROM project_items pi JOIN projects p ON p.id=pi.project_id "
                           "WHERE pi.item_id=?", (item_id,))
    if user_id is not None:
        prog = db.q1("SELECT position, duration, completed FROM progress WHERE user_id=? AND item_id=?",
                     (user_id, item_id))
        if prog:
            prog["completed"] = bool(prog["completed"])  # SQLite stores 0/1; clients expect a boolean
        out["progress"] = prog
    if row["group_key"]:
        out["siblings"] = db.q1("SELECT COUNT(*) c FROM items WHERE group_key=? AND trashed_at IS NULL",
                                (row["group_key"],))["c"]
    return out


def search(query: str | None = None, kind: str | list[str] | None = None, library_id: int | None = None,
           tag: str | None = None, date_from: float | None = None, date_to: float | None = None,
           project_id: int | None = None, group: str | None = None, favorite: bool | None = None,
           sort: str = "relevance", limit: int = 50, offset: int = 0, include_trashed: bool = False) -> list[dict]:
    where = ["1=1"] if include_trashed else ["i.trashed_at IS NULL"]
    params: list = []
    join = ""
    order = "i.taken_at DESC"
    if query:
        from .db import fts_query

        fq = fts_query(query)
        if fq:
            join += " JOIN items_fts f ON f.rowid=i.id"
            where.append("items_fts MATCH ?")
            params.append(fq)
            if sort == "relevance":
                order = "bm25(items_fts, 10.0, 2.0, 5.0, 1.0)"
    if kind:
        ks = [kind] if isinstance(kind, str) else list(kind)
        where.append(f"i.kind IN ({','.join('?' * len(ks))})")
        params += ks
    if library_id:
        where.append("i.library_id=?")
        params.append(library_id)
    if tag:
        where.append("EXISTS (SELECT 1 FROM tags t WHERE t.item_id=i.id AND t.tag=?)")
        params.append(tag)
    if date_from:
        where.append("i.taken_at>=?")
        params.append(date_from)
    if date_to:
        where.append("i.taken_at<?")
        params.append(date_to)
    if project_id:
        where.append("EXISTS (SELECT 1 FROM project_items pi WHERE pi.item_id=i.id AND pi.project_id=?)")
        params.append(project_id)
    if group:
        where.append("i.group_key=?")
        params.append(group)
    if favorite is not None:
        where.append("i.favorite=?")
        params.append(1 if favorite else 0)
    order = {
        "newest": "i.taken_at DESC", "oldest": "i.taken_at ASC", "added": "i.added_at DESC",
        "title": "i.title COLLATE NOCASE", "size": "i.size DESC", "group": "i.group_key, i.sort_key",
        "duration": "i.duration DESC",
    }.get(sort, order)
    sql = (f"SELECT {ITEM_COLS} FROM items i JOIN libraries l ON l.id=i.library_id {join} "
           f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?")
    return cards(db.q(sql, params + [min(max(limit, 1), 500), max(offset, 0)]))


def continue_watching(user_id: int, limit: int = 20) -> list[dict]:
    rows = db.q(f"SELECT {ITEM_COLS}, p.position, p.completed FROM progress p JOIN items i ON i.id=p.item_id "
                f"JOIN libraries l ON l.id=i.library_id WHERE p.user_id=? AND p.completed=0 AND p.position>15 "
                f"AND i.trashed_at IS NULL ORDER BY p.updated_at DESC LIMIT ?", (user_id, limit))
    return cards(rows, user_id)


def recently_added(kinds: list[str] | None = None, limit: int = 24, library_id: int | None = None) -> list[dict]:
    return search(kind=kinds, library_id=library_id, sort="added", limit=limit)


def groups(library_id: int | None = None, prefix: str | None = None, kind: list[str] | None = None,
           limit: int = 200, offset: int = 0) -> list[dict]:
    """Shows, albums, shoot days, part families — one row per group with a representative item."""
    where = ["i.trashed_at IS NULL", "i.group_key IS NOT NULL"]
    params: list = []
    if library_id:
        where.append("i.library_id=?")
        params.append(library_id)
    if prefix:
        where.append("i.group_key LIKE ?")
        params.append(prefix + "%")
    if kind:
        where.append(f"i.kind IN ({','.join('?' * len(kind))})")
        params += kind
    rows = db.q(
        f"SELECT i.group_key, COUNT(*) AS count, SUM(i.size) AS size, SUM(COALESCE(i.duration,0)) AS duration, "
        f"MIN(i.taken_at) AS first, MAX(i.taken_at) AS last, MAX(i.added_at) AS added, "
        f"(SELECT j.id FROM items j WHERE j.group_key=i.group_key AND j.thumb_state='ok' AND j.trashed_at IS NULL "
        f" ORDER BY j.sort_key LIMIT 1) AS cover_id "
        f"FROM items i WHERE {' AND '.join(where)} GROUP BY i.group_key ORDER BY last DESC LIMIT ? OFFSET ?",
        params + [limit, offset])
    for r in rows:
        r["name"] = group_label(r["group_key"])
        r["first_date"], r["last_date"] = _iso(r["first"]), _iso(r["last"])
    return rows


def group_label(key: str) -> str:
    kind, _, rest = key.partition(":")
    if kind == "album":
        artist, _, album = rest.partition(":")
        return f"{album.title()} — {artist.title()}"
    if kind in ("show", "movie"):
        return rest.split(":")[0].title()
    if kind in ("shoot", "day"):
        try:
            d = datetime.strptime(rest, "%Y-%m-%d")
            return f"{d:%a %b} {d.day}, {d.year}"
        except ValueError:
            return rest
    if kind == "eng":
        folder, _, name = rest.rpartition("/")
        return f"{name} ({folder})" if folder and folder != "." else name
    if kind == "folder":
        return rest or "/"
    return key


def stats() -> dict:
    by_kind = {r["kind"]: {"count": r["c"], "size": r["s"], "duration": r["d"]} for r in db.q(
        "SELECT kind, COUNT(*) c, SUM(size) s, SUM(COALESCE(duration,0)) d FROM items WHERE trashed_at IS NULL GROUP BY kind")}
    return {
        "items": sum(v["count"] for v in by_kind.values()),
        "bytes": sum(v["size"] or 0 for v in by_kind.values()),
        "by_kind": by_kind,
        "libraries": db.q1("SELECT COUNT(*) c FROM libraries")["c"],
        "projects": db.q1("SELECT COUNT(*) c FROM projects")["c"],
        "trash": db.q1("SELECT COUNT(*) c FROM items WHERE trashed_at IS NOT NULL")["c"],
    }


def on_this_day(limit: int = 30) -> list[dict]:
    now = datetime.now()
    md = now.strftime("%m-%d")
    rows = db.q(f"SELECT {ITEM_COLS} FROM items i JOIN libraries l ON l.id=i.library_id "
                f"WHERE i.kind IN ('photo','video') AND i.trashed_at IS NULL AND i.thumb_state='ok' "
                f"AND strftime('%m-%d', i.taken_at, 'unixepoch', 'localtime')=? "
                f"AND strftime('%Y', i.taken_at, 'unixepoch', 'localtime')<? ORDER BY RANDOM() LIMIT ?",
                (md, str(now.year), limit))
    return cards(rows)
