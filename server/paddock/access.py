"""Who may see and change what: communities, library access, and media-only mode.

Communities are groups of people (a household, the race team, the friends from college). They decide:
- which libraries their members see and can add to: a library is either open to everyone (the default, as
  before communities existed) or limited to the communities it's shared with, each with read or write access;
- which projects members can open and work on;
- where comments, watch parties and voice chat happen (each can be switched off per community).

Media-only mode (for one person, or for everyone who isn't an admin) keeps someone to movies, shows, music,
photos and home videos: no engineering files, RED footage, projects, AI assistant or agent tokens. Watch
parties, voice chat and comments still work.

Admins always see everything. Everything here is checked on the server for every request.
"""

from __future__ import annotations

import json

from . import db
from .actor import Actor

MEDIA_KINDS = {"movies", "shows", "music", "photos", "home_videos"}
COMMUNITY_DEFAULTS = {"comments": True, "parties": True, "voice": True}


def _everything(actor: Actor | None) -> bool:
    return actor is None or actor.kind == "system" or actor.is_admin


def media_only(user_id: int) -> bool:
    if db.get_setting("media_only_everyone", False):
        u = db.q1("SELECT is_admin FROM users WHERE id=?", (user_id,))
        if u and not u["is_admin"]:
            return True
    row = db.q1("SELECT media_only FROM user_settings WHERE user_id=?", (user_id,))
    return bool(row and row["media_only"])


def communities_of(user_id: int) -> list[int]:
    return [r["community_id"] for r in db.q("SELECT community_id FROM community_members WHERE user_id=?", (user_id,))]


def _visibility(options: str | None) -> str:
    try:
        return json.loads(options or "{}").get("visibility", "everyone")
    except ValueError:
        return "everyone"


def library_access(actor: Actor | None) -> dict[int, str] | None:
    """library id → "read" | "write" for this person, or None when they may see everything (admins)."""
    if _everything(actor):
        return None
    mine = communities_of(actor.user_id)
    grants: dict[int, str] = {}
    if mine:
        for r in db.q(f"SELECT library_id, access FROM library_access WHERE community_id IN ({','.join('?' * len(mine))})", mine):
            if grants.get(r["library_id"]) != "write":
                grants[r["library_id"]] = r["access"]
    limited = media_only(actor.user_id)
    out: dict[int, str] = {}
    photos: set[int] = set()
    for lib in db.q("SELECT id, kind, options FROM libraries"):
        if lib["kind"] == "photos":
            photos.add(lib["id"])
        if limited and lib["kind"] not in MEDIA_KINDS:
            continue
        if _visibility(lib["options"]) == "everyone":
            out[lib["id"]] = "write"            # open libraries: everyone reads and writes, as before communities
        elif lib["id"] in grants:
            out[lib["id"]] = grants[lib["id"]]
    if limited:   # media-only people watch; they don't reorganise libraries (their phone backups still arrive)
        out = {k: v if k in photos else "read" for k, v in out.items()}
    return out


def library_ids(actor: Actor | None) -> set[int] | None:
    acc = library_access(actor)
    return None if acc is None else set(acc)


def clause(allowed: set[int] | None, column: str = "i.library_id") -> tuple[str, list]:
    """SQL to add to a WHERE: nothing for admins, otherwise only the allowed libraries."""
    if allowed is None:
        return "", []
    if not allowed:
        return " AND 0", []
    ids = sorted(allowed)
    return f" AND {column} IN ({','.join('?' * len(ids))})", ids


def can_read_library(actor: Actor | None, library_id: int) -> bool:
    acc = library_access(actor)
    return acc is None or library_id in acc


def can_write_library(actor: Actor | None, library_id: int) -> bool:
    acc = library_access(actor)
    return acc is None or acc.get(library_id) == "write"


def can_read_item(actor: Actor | None, item: dict) -> bool:
    if can_read_library(actor, item["library_id"]):
        return True
    # a watch party shares what it's playing with everyone in it
    from .party import parties

    return any(p.item_id == item["id"] and not p.ended
               and (actor.user_id == p.host_id or any(m.user_id == actor.user_id for m in list(p.members.values())))
               for p in list(parties.parties.values()))


# ---- projects ---------------------------------------------------------------------------------

def project_access(actor: Actor | None, project: dict) -> str | None:
    """"write", "read" or None for one project."""
    if _everything(actor):
        return "write"
    if media_only(actor.user_id):
        return None
    rows = db.q("SELECT access, community_id FROM project_access WHERE project_id=?", (project["id"],))
    if not rows:
        return "write"                          # projects not shared with a community are open to everyone
    mine = set(communities_of(actor.user_id))
    levels = {r["access"] for r in rows if r["community_id"] in mine}
    return "write" if "write" in levels else "read" if levels else None


# ---- communities --------------------------------------------------------------------------------

def community_settings(row: dict) -> dict:
    try:
        return COMMUNITY_DEFAULTS | json.loads(row.get("settings") or "{}")
    except ValueError:
        return dict(COMMUNITY_DEFAULTS)


def shared_communities(a: int, b: int, feature: str | None = None) -> list[int]:
    """Communities both people are in (optionally only those with a feature switched on)."""
    rows = db.q("SELECT c.id, c.settings FROM communities c JOIN community_members x ON x.community_id=c.id AND x.user_id=? "
                "JOIN community_members y ON y.community_id=c.id AND y.user_id=?", (a, b))
    return [r["id"] for r in rows if feature is None or community_settings(r).get(feature, True)]


def my_communities(user_id: int, feature: str | None = None) -> list[dict]:
    rows = db.q("SELECT c.*, m.role FROM communities c JOIN community_members m ON m.community_id=c.id WHERE m.user_id=? "
                "ORDER BY c.name COLLATE NOCASE", (user_id,))
    out = []
    for r in rows:
        s = community_settings(r)
        if feature is None or s.get(feature, True):
            out.append({"id": r["id"], "name": r["name"], "description": r["description"], "role": r["role"], "settings": s})
    return out


def is_member(user_id: int, community_id: int) -> bool:
    return bool(db.q1("SELECT 1 FROM community_members WHERE user_id=? AND community_id=?", (user_id, community_id)))
