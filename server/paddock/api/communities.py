"""Communities: groups of people that share libraries, projects, comments, watch parties and voice (see access.py)."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import access, db
from ..actor import Actor
from .deps import current_actor, require_admin

router = APIRouter(prefix="/api/communities")


class CommunityBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    settings: dict = Field(default_factory=dict)
    members: list[int] = Field(default_factory=list, max_length=1000)


class CommunityPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    settings: dict | None = None


class MembersBody(BaseModel):
    members: list[dict] = Field(max_length=1000)          # [{user_id, role: member|manager}]


def _clean_settings(s: dict) -> dict:
    return {k: bool(s[k]) for k in access.COMMUNITY_DEFAULTS if k in s}


def _full(cid: int) -> dict:
    c = db.q1("SELECT * FROM communities WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "No such community")
    c["settings"] = access.community_settings(c)
    c["members"] = db.q("SELECT u.id AS user_id, u.username, u.display_name, m.role FROM community_members m "
                        "JOIN users u ON u.id=m.user_id WHERE m.community_id=? ORDER BY u.username COLLATE NOCASE", (cid,))
    c["libraries"] = db.q("SELECT l.id, l.name, a.access FROM library_access a JOIN libraries l ON l.id=a.library_id "
                          "WHERE a.community_id=? ORDER BY l.name", (cid,))
    c["projects"] = db.q("SELECT p.id, p.name, a.access FROM project_access a JOIN projects p ON p.id=a.project_id "
                         "WHERE a.community_id=? ORDER BY p.name", (cid,))
    return c


def _may_manage(actor: Actor, cid: int) -> None:
    if actor.is_admin:
        return
    if not db.q1("SELECT 1 FROM community_members WHERE community_id=? AND user_id=? AND role='manager'", (cid, actor.user_id)):
        raise HTTPException(403, "Only an admin or this community's managers can change it")


@router.get("")
def list_communities(actor: Actor = Depends(current_actor)):
    if actor.is_admin:
        return [_full(r["id"]) for r in db.q("SELECT id FROM communities ORDER BY name COLLATE NOCASE")]
    return [_full(c["id"]) | {"role": c["role"]} for c in access.my_communities(actor.user_id)]


@router.post("")
def create(body: CommunityBody, actor: Actor = Depends(require_admin)):
    if db.q1("SELECT 1 FROM communities WHERE name=?", (body.name.strip(),)):
        raise HTTPException(409, "There's already a community with that name")
    with db.tx():
        cid = db.x("INSERT INTO communities(name, description, settings, created_at) VALUES(?,?,?,?)",
                   (body.name.strip(), body.description.strip(), json.dumps(_clean_settings(body.settings)), time.time())).lastrowid
        for uid in set(body.members):
            if db.q1("SELECT 1 FROM users WHERE id=?", (uid,)):
                db.x("INSERT OR IGNORE INTO community_members(community_id, user_id) VALUES(?,?)", (cid, uid))
    db.audit(actor.audit_name, "community.created", community=cid, name=body.name)
    return _full(cid)


@router.get("/{cid}")
def get(cid: int, actor: Actor = Depends(current_actor)):
    if not actor.is_admin and not access.is_member(actor.user_id, cid):
        raise HTTPException(404, "No such community")
    return _full(cid)


@router.patch("/{cid}")
def update(cid: int, body: CommunityPatch, actor: Actor = Depends(current_actor)):
    _may_manage(actor, cid)
    c = _full(cid)
    if body.name and body.name.strip() != c["name"]:
        if db.q1("SELECT 1 FROM communities WHERE name=? AND id!=?", (body.name.strip(), cid)):
            raise HTTPException(409, "There's already a community with that name")
        db.x("UPDATE communities SET name=? WHERE id=?", (body.name.strip(), cid))
    if body.description is not None:
        db.x("UPDATE communities SET description=? WHERE id=?", (body.description.strip(), cid))
    if body.settings is not None:
        db.x("UPDATE communities SET settings=? WHERE id=?", (json.dumps(c["settings"] | _clean_settings(body.settings)), cid))
    db.audit(actor.audit_name, "community.updated", community=cid)
    return _full(cid)


@router.put("/{cid}/members")
def set_members(cid: int, body: MembersBody, actor: Actor = Depends(current_actor)):
    _may_manage(actor, cid)
    _full(cid)
    rows = []
    for m in body.members:
        uid, role = int(m.get("user_id", 0)), m.get("role", "member")
        if role not in ("member", "manager") or not db.q1("SELECT 1 FROM users WHERE id=?", (uid,)):
            raise HTTPException(400, "Unknown person or role")
        if role == "manager" and not actor.is_admin:
            role = "member" if not db.q1("SELECT 1 FROM community_members WHERE community_id=? AND user_id=? AND role='manager'",
                                         (cid, uid)) else "manager"
        rows.append((cid, uid, role))
    with db.tx():
        db.x("DELETE FROM community_members WHERE community_id=?", (cid,))
        for r in rows:
            db.x("INSERT OR REPLACE INTO community_members(community_id, user_id, role) VALUES(?,?,?)", r)
    db.audit(actor.audit_name, "community.members", community=cid, members=[r[1] for r in rows])
    return _full(cid)


@router.delete("/{cid}")
def delete(cid: int, actor: Actor = Depends(require_admin)):
    _full(cid)
    db.x("DELETE FROM communities WHERE id=?", (cid,))
    db.audit(actor.audit_name, "community.deleted", community=cid)
    return {"ok": True}
