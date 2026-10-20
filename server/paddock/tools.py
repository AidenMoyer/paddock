"""The tool surface AI agents get — exposed over MCP and to the built-in assistant.

Design rules:
  * Every tool declares a scope (read / write / admin). Tokens and users only see
    tools their scopes allow; the in-app assistant only gets write tools when the
    user has switched on "Let the assistant make changes".
  * Nothing here can permanently delete a file. The most destructive action is
    moving items to Paddock's trash, which a human empties from the web UI.
  * Every write is recorded in the audit log with the agent's identity.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import __version__, catalog, db, jobs, projects
from .actor import Actor
from .config import settings


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[[Actor, dict], Any]
    scope: str = "read"
    destructive: bool = False

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None,
         scope: str = "read", destructive: bool = False):
    schema = {"type": "object", "properties": properties or {}, "required": required or [],
              "additionalProperties": False}

    def deco(fn):
        TOOLS[name] = Tool(name, description, schema, fn, scope, destructive)
        return fn

    return deco


class ToolError(Exception):
    pass


def available(actor: Actor) -> list[Tool]:
    return [t for t in TOOLS.values() if actor.can(t.scope)]


def call(actor: Actor, name: str, args: dict) -> Any:
    t = TOOLS.get(name)
    if t is None:
        raise ToolError(f"Unknown tool: {name}")
    if not actor.can(t.scope):
        raise ToolError(f"'{name}' needs the '{t.scope}' permission, which this connection does not have.")
    _validate(t.schema, args or {})
    result = t.fn(actor, args or {})
    if t.scope != "read":
        db.audit(actor.audit_name, f"tool.{name}", args=args)
    return result


def _validate(schema: dict, args: dict) -> None:
    props = schema.get("properties", {})
    for r in schema.get("required", []):
        if r not in args:
            raise ToolError(f"Missing required argument '{r}'.")
    for k, v in args.items():
        if k not in props:
            raise ToolError(f"Unknown argument '{k}'.")
        typ = props[k].get("type")
        ok = {
            "string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool), "boolean": isinstance(v, bool),
            "array": isinstance(v, list), "object": isinstance(v, dict),
        }.get(typ, True)
        if not ok:
            raise ToolError(f"Argument '{k}' must be a {typ}.")
        if typ == "array" and props[k].get("items", {}).get("type") == "integer":
            if not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
                raise ToolError(f"Argument '{k}' must be a list of integers.")
            if len(v) > 500:
                raise ToolError(f"Argument '{k}' may contain at most 500 ids.")
        if "enum" in props[k] and v not in props[k]["enum"]:
            raise ToolError(f"Argument '{k}' must be one of: {', '.join(map(str, props[k]['enum']))}.")


def _date(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        raise ToolError(f"Dates must look like 2026-07-04 (got '{s}').")


def _ids(args: dict, key: str = "item_ids") -> list[int]:
    ids = args.get(key) or []
    found = {r["id"] for r in db.q(f"SELECT id FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
    missing = [i for i in ids if i not in found]
    if missing:
        raise ToolError(f"No such item id(s): {missing[:20]}")
    return ids


KINDS = ["video", "audio", "photo", "red_clip", "braw_clip", "model3d", "gcode", "cad", "pcb", "document"]
ID_LIST = {"type": "array", "items": {"type": "integer"}}

# ---- read tools ---------------------------------------------------------------------


@tool("search_media",
      "Search everything in the vault — movies, shows, music, photos, home videos, RED/cinema clips, CAD, "
      "3D-print and electronics files. Free-text matches titles, paths, tags and metadata. Combine with filters. "
      "Returns compact item cards with ids you can pass to other tools.",
      {"query": {"type": "string", "description": "Free text, e.g. 'turbo manifold' or 'beach sunset'"},
       "kind": {"type": "string", "enum": KINDS},
       "library_id": {"type": "integer"},
       "tag": {"type": "string"},
       "date_from": {"type": "string", "description": "ISO date, inclusive"},
       "date_to": {"type": "string", "description": "ISO date, exclusive"},
       "project_id": {"type": "integer"},
       "sort": {"type": "string", "enum": ["relevance", "newest", "oldest", "added", "title", "size", "duration"]},
       "limit": {"type": "integer", "description": "Max results (default 25, max 200)"}})
def _search_media(actor: Actor, a: dict):
    return catalog.search(query=a.get("query"), kind=a.get("kind"), library_id=a.get("library_id"),
                          tag=a.get("tag"), date_from=_date(a.get("date_from")), date_to=_date(a.get("date_to")),
                          project_id=a.get("project_id"), sort=a.get("sort", "relevance"),
                          limit=min(a.get("limit", 25), 200))


@tool("get_item", "Full details for one item: metadata (EXIF, codecs, RED clip info, print stats...), tags, "
      "projects it belongs to.", {"item_id": {"type": "integer"}}, ["item_id"])
def _get_item(actor: Actor, a: dict):
    item = catalog.get_item(a["item_id"], actor.user_id)
    if not item:
        raise ToolError("Item not found.")
    return item


@tool("list_libraries", "List the media libraries (folders on the NAS) with their type and item counts.")
def _list_libraries(actor: Actor, a: dict):
    return db.q("SELECT id, name, kind, item_count, scan_state, last_scan FROM libraries ORDER BY name")


@tool("library_overview", "Totals by media type, total size, trash count and number of projects.")
def _overview(actor: Actor, a: dict):
    return catalog.stats()


@tool("list_groups",
      "List groupings: TV shows ('show:'), albums ('album:'), photo days ('day:'), RED shoot days ('shoot:'), "
      "CAD part families with all their revisions/exports ('eng:'). Use the returned group_key with "
      "search_media-like browsing via get_group.",
      {"prefix": {"type": "string", "enum": ["show:", "album:", "day:", "shoot:", "eng:", "movie:", "folder:"]},
       "library_id": {"type": "integer"}, "limit": {"type": "integer"}}, ["prefix"])
def _list_groups(actor: Actor, a: dict):
    return catalog.groups(library_id=a.get("library_id"), prefix=a["prefix"], limit=min(a.get("limit", 50), 300))


@tool("get_group", "All items in one group (an album, a show, a shoot day, a part family).",
      {"group_key": {"type": "string"}, "limit": {"type": "integer"}}, ["group_key"])
def _get_group(actor: Actor, a: dict):
    return catalog.search(group=a["group_key"], sort="group", limit=min(a.get("limit", 100), 500))


@tool("server_status", "Health of the server: disk space per media root, CPU load, memory, running jobs, versions.")
def _server_status(actor: Actor, a: dict):
    disks = []
    for root in settings.media_roots + [settings.data_dir]:
        try:
            u = shutil.disk_usage(root)
            disks.append({"path": str(root), "total_gb": round(u.total / 1e9, 1), "used_gb": round(u.used / 1e9, 1),
                          "free_gb": round(u.free / 1e9, 1), "used_pct": round(100 * u.used / u.total, 1)})
        except OSError:
            disks.append({"path": str(root), "error": "unavailable"})
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    mem = None
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        mem = {"total_gb": round(int(info["MemTotal"].split()[0]) / 1e6, 1),
               "available_gb": round(int(info["MemAvailable"].split()[0]) / 1e6, 1)}
    except (OSError, KeyError, ValueError):
        pass
    from .transcode import active_hwaccel, segments

    return {"version": __version__, "disks": disks, "load_avg": load, "memory": mem, "cpu_count": os.cpu_count(),
            "active_jobs": jobs.active(), "active_transcodes": len(segments.status()), "hwaccel": active_hwaccel(),
            "wireguard": settings.wg_enabled, "time": datetime.now().isoformat(timespec="seconds")}


@tool("list_jobs", "Recent background jobs (scans, offloads, proxy renders) and their status.")
def _list_jobs(actor: Actor, a: dict):
    return jobs.recent(30)


@tool("list_projects", "List projects (builds, trips, race weekends...) with item counts and status.")
def _list_projects(actor: Actor, a: dict):
    return projects.listing()


@tool("get_project", "Everything in one project: items with roles and notes, a day-by-day summary, notes, and "
      "the current video plan.", {"project_id": {"type": "integer"}}, ["project_id"])
def _get_project(actor: Actor, a: dict):
    p = projects.detail(a["project_id"])
    if not p:
        raise ToolError("Project not found.")
    return p


@tool("find_duplicates", "Find likely duplicate files (same size and same name, or same checksum) — read only.",
      {"library_id": {"type": "integer"}, "limit": {"type": "integer"}})
def _dupes(actor: Actor, a: dict):
    where, params = "trashed_at IS NULL", []
    if a.get("library_id"):
        where += " AND library_id=?"
        params.append(a["library_id"])
    rows = db.q(f"SELECT size, lower(title) t, GROUP_CONCAT(id) ids, COUNT(*) n FROM items WHERE {where} "
                f"GROUP BY size, lower(title) HAVING n>1 ORDER BY size DESC LIMIT ?", params + [min(a.get("limit", 50), 200)])
    return [{"title": r["t"], "size": r["size"], "item_ids": [int(x) for x in r["ids"].split(",")]} for r in rows]


@tool("recent_activity", "What happened recently: uploads, scans, inbox filing and changes (audit log).",
      {"limit": {"type": "integer"}})
def _activity(actor: Actor, a: dict):
    rows = db.q("SELECT ts, actor, action, detail FROM audit_log ORDER BY id DESC LIMIT ?", (min(a.get("limit", 30), 200),))
    for r in rows:
        r["time"] = datetime.fromtimestamp(r.pop("ts")).isoformat(timespec="seconds")
        r["detail"] = json.loads(r["detail"])
    return rows


# ---- write tools ----------------------------------------------------------------------


@tool("add_tags", "Add tags to items (e.g. 'turbo build', 'b-roll', 'needs color', 'PETG').",
      {"item_ids": ID_LIST, "tags": {"type": "array", "items": {"type": "string"}}}, ["item_ids", "tags"], scope="write")
def _add_tags(actor: Actor, a: dict):
    ids = _ids(a)
    tags = [t.strip()[:60] for t in a["tags"] if t.strip()]
    source = "ai" if actor.kind in ("token", "ai") else "user"
    with db.tx():
        for iid in ids:
            for t in tags:
                db.x("INSERT OR IGNORE INTO tags(item_id, tag, source) VALUES(?,?,?)", (iid, t, source))
            db.fts_index(iid)
    return {"tagged": len(ids), "tags": tags}


@tool("remove_tags", "Remove tags from items.",
      {"item_ids": ID_LIST, "tags": {"type": "array", "items": {"type": "string"}}}, ["item_ids", "tags"], scope="write")
def _remove_tags(actor: Actor, a: dict):
    ids = _ids(a)
    with db.tx():
        for iid in ids:
            for t in a["tags"]:
                db.x("DELETE FROM tags WHERE item_id=? AND tag=?", (iid, t))
            db.fts_index(iid)
    return {"updated": len(ids)}


@tool("rename_item", "Change an item's display title in Paddock (the file on disk is not renamed).",
      {"item_id": {"type": "integer"}, "title": {"type": "string"}}, ["item_id", "title"], scope="write")
def _rename(actor: Actor, a: dict):
    _ids({"item_ids": [a["item_id"]]})
    db.x("UPDATE items SET title=? WHERE id=?", (a["title"].strip()[:300], a["item_id"]))
    db.fts_index(a["item_id"])
    return {"ok": True}


@tool("set_favorite", "Mark or unmark items as favorites.",
      {"item_ids": ID_LIST, "favorite": {"type": "boolean"}}, ["item_ids", "favorite"], scope="write")
def _fav(actor: Actor, a: dict):
    ids = _ids(a)
    db.x(f"UPDATE items SET favorite=? WHERE id IN ({','.join('?' * len(ids))})", (1 if a["favorite"] else 0, *ids))
    return {"updated": len(ids)}


@tool("create_collection", "Create a named collection (playlist / album / selects reel) from item ids.",
      {"name": {"type": "string"}, "description": {"type": "string"}, "item_ids": ID_LIST}, ["name", "item_ids"],
      scope="write")
def _collection(actor: Actor, a: dict):
    ids = _ids(a)
    with db.tx():
        cid = db.x("INSERT INTO collections(name, description, created_by, created_at) VALUES(?,?,?,?)",
                   (a["name"][:200], a.get("description", ""), actor.audit_name, time.time())).lastrowid
        for pos, iid in enumerate(ids):
            db.x("INSERT OR IGNORE INTO collection_items(collection_id, item_id, position) VALUES(?,?,?)", (cid, iid, pos))
    return {"collection_id": cid, "items": len(ids)}


@tool("create_project", "Start a project to gather footage, photos, sounds, CAD and notes for something you're "
      "building or doing — and plan a video about it.",
      {"name": {"type": "string"}, "description": {"type": "string"}, "goal": {"type": "string"},
       "location_name": {"type": "string"},
       "status": {"type": "string", "enum": list(projects.STATUSES)}}, ["name"], scope="write")
def _create_project(actor: Actor, a: dict):
    pid = projects.create(a["name"], a.get("description", ""), a.get("goal", ""), a.get("location_name"),
                          status=a.get("status", "active"))
    return {"project_id": pid}


@tool("update_project", "Edit a project's name, description, goal, location or status.",
      {"project_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"},
       "goal": {"type": "string"}, "location_name": {"type": "string"},
       "status": {"type": "string", "enum": list(projects.STATUSES)}}, ["project_id"], scope="write")
def _update_project(actor: Actor, a: dict):
    a = dict(a)
    pid = a.pop("project_id")
    if not db.q1("SELECT 1 FROM projects WHERE id=?", (pid,)):
        raise ToolError("Project not found.")
    projects.update(pid, **a)
    return {"ok": True}


@tool("add_to_project", "Attach items to a project with a role and optional note (e.g. 'great hook shot').",
      {"project_id": {"type": "integer"}, "item_ids": ID_LIST,
       "role": {"type": "string", "enum": list(projects.ROLES)}, "note": {"type": "string"}},
      ["project_id", "item_ids"], scope="write")
def _add_to_project(actor: Actor, a: dict):
    if not db.q1("SELECT 1 FROM projects WHERE id=?", (a["project_id"],)):
        raise ToolError("Project not found.")
    n = projects.add_items(a["project_id"], _ids(a), a.get("role", "footage"), a.get("note", ""))
    return {"added": n}


@tool("add_project_note", "Add a note to a project (ideas, to-dos, shot ideas, measurements).",
      {"project_id": {"type": "integer"}, "body": {"type": "string"}}, ["project_id", "body"], scope="write")
def _note(actor: Actor, a: dict):
    if not db.q1("SELECT 1 FROM projects WHERE id=?", (a["project_id"],)):
        raise ToolError("Project not found.")
    return {"note_id": projects.add_note(a["project_id"], a["body"], actor.audit_name)}


@tool("save_video_plan", "Save (replace) the project's video plan as Markdown: outline, shot list referencing "
      "item ids like #123, missing shots, voice-over draft, music.",
      {"project_id": {"type": "integer"}, "plan_markdown": {"type": "string"}}, ["project_id", "plan_markdown"],
      scope="write")
def _save_plan(actor: Actor, a: dict):
    if not db.q1("SELECT 1 FROM projects WHERE id=?", (a["project_id"],)):
        raise ToolError("Project not found.")
    projects.save_plan(a["project_id"], a["plan_markdown"])
    return {"ok": True}


@tool("scan_library", "Rescan a library folder for new, changed or removed files.",
      {"library_id": {"type": "integer"}}, ["library_id"], scope="write")
def _scan(actor: Actor, a: dict):
    from . import scanner

    if not db.q1("SELECT 1 FROM libraries WHERE id=?", (a["library_id"],)):
        raise ToolError("Library not found.")
    return {"job_id": scanner.queue_scan(a["library_id"])}


@tool("move_to_trash", "Hide items by moving them to Paddock's trash. Files stay on disk until a person empties "
      "the trash in the web app; this can be undone with restore_from_trash.",
      {"item_ids": ID_LIST, "reason": {"type": "string"}}, ["item_ids", "reason"], scope="write", destructive=True)
def _trash(actor: Actor, a: dict):
    ids = _ids(a)
    db.x(f"UPDATE items SET trashed_at=? WHERE id IN ({','.join('?' * len(ids))})", (time.time(), *ids))
    return {"trashed": len(ids), "note": "Recoverable from Trash in the web app."}


@tool("restore_from_trash", "Restore items from Paddock's trash.", {"item_ids": ID_LIST}, ["item_ids"], scope="write")
def _restore(actor: Actor, a: dict):
    ids = a["item_ids"]
    db.x(f"UPDATE items SET trashed_at=NULL WHERE id IN ({','.join('?' * len(ids))})", ids)
    return {"restored": len(ids)}


@tool("send_to_printer", "Send a G-code file to a 3D printer on the home network (Klipper/Moonraker or OctoPrint) "
      "configured in Settings. Uploads only; does not start the print unless start=true.",
      {"item_id": {"type": "integer"}, "printer": {"type": "string"}, "start": {"type": "boolean"}},
      ["item_id", "printer"], scope="write")
def _print(actor: Actor, a: dict):
    from . import printers

    return printers.send(a["item_id"], a["printer"], bool(a.get("start", False)))


# ---- admin tools ------------------------------------------------------------------------


@tool("list_devices", "Paired phones/tablets, when they were last seen, and whether they use the tunnel.",
      scope="admin")
def _devices(actor: Actor, a: dict):
    return db.q("SELECT d.id, d.name, d.platform, d.wg_ip, d.created_at, d.last_seen, d.revoked_at, u.username "
                "FROM devices d JOIN users u ON u.id=d.user_id ORDER BY d.id")
