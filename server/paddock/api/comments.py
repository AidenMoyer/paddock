"""Comments on videos, photos and everything else.

Each comment lives in one place: a community (seen by its members, if that community has comments switched
on) or the whole server (seen by everyone who can see the item, if the admin allows server-wide comments).
A comment can point at a moment in a video. Nobody sees a comment on something they can't open.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import access, db, events
from ..actor import Actor
from .deps import current_actor
from .media import _item_or_404

router = APIRouter(prefix="/api")


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    community_id: int | None = None          # None: everyone on the server
    at: float | None = Field(default=None, ge=0)


class CommentPatch(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


def everyone_allowed() -> bool:
    return bool(db.get_setting("comments_everyone", True))


def places(actor: Actor) -> list[dict]:
    """Where this person can comment (and so read comments)."""
    out = [{"id": None, "name": "Everyone"}] if everyone_allowed() else []
    return out + [{"id": c["id"], "name": c["name"]} for c in access.my_communities(actor.user_id, "comments")]


def _visible_clause(actor: Actor) -> tuple[str, list]:
    if actor.is_admin:
        return "", []
    ids = [c["id"] for c in access.my_communities(actor.user_id, "comments")]
    parts = ["c.community_id IS NULL"] if everyone_allowed() else []
    if ids:
        parts.append(f"c.community_id IN ({','.join('?' * len(ids))})")
    if not parts:
        return " AND 0", []
    return f" AND ({' OR '.join(parts)})", ids


def _public(r: dict, actor: Actor) -> dict:
    return {"id": r["id"], "item_id": r["item_id"], "body": r["body"], "at": r["at"], "created_at": r["created_at"],
            "edited_at": r["edited_at"], "user_id": r["user_id"], "username": r["username"],
            "display_name": r["display_name"] or r["username"], "community_id": r["community_id"],
            "community": r["community"] or "Everyone", "mine": r["user_id"] == actor.user_id}


SELECT = ("SELECT c.*, u.username, u.display_name, m.name AS community FROM comments c JOIN users u ON u.id=c.user_id "
          "LEFT JOIN communities m ON m.id=c.community_id")


@router.get("/items/{item_id}/comments")
def list_comments(item_id: int, actor: Actor = Depends(current_actor)):
    _item_or_404(item_id, actor)
    frag, params = _visible_clause(actor)
    rows = db.q(f"{SELECT} WHERE c.item_id=?{frag} ORDER BY c.created_at", (item_id, *params))
    return {"comments": [_public(r, actor) for r in rows], "places": places(actor)}


@router.post("/items/{item_id}/comments")
def add_comment(item_id: int, body: CommentBody, actor: Actor = Depends(current_actor)):
    if actor.kind not in ("web", "device"):
        raise HTTPException(403, "Only people can comment")
    _item_or_404(item_id, actor)
    if body.community_id is None:
        if not everyone_allowed():
            raise HTTPException(403, "Comments for everyone are switched off on this server. Pick a community.")
    elif body.community_id not in [c["id"] for c in access.my_communities(actor.user_id, "comments")]:
        raise HTTPException(403, "You can't comment in that community")
    cid = db.x("INSERT INTO comments(item_id, community_id, user_id, body, at, created_at) VALUES(?,?,?,?,?,?)",
               (item_id, body.community_id, actor.user_id, body.body.strip(), body.at, time.time())).lastrowid
    row = db.q1(f"{SELECT} WHERE c.id=?", (cid,))
    out = _public(row, actor)
    events.publish_comment(out)
    return out


def _own(comment_id: int, actor: Actor, delete: bool = False) -> dict:
    r = db.q1("SELECT * FROM comments WHERE id=?", (comment_id,))
    if not r:
        raise HTTPException(404, "No such comment")
    if r["user_id"] == actor.user_id or actor.is_admin:
        return r
    if delete and r["community_id"] and db.q1("SELECT 1 FROM community_members WHERE community_id=? AND user_id=? "
                                              "AND role='manager'", (r["community_id"], actor.user_id)):
        return r
    raise HTTPException(403, "That's someone else's comment")


@router.patch("/comments/{comment_id}")
def edit_comment(comment_id: int, body: CommentPatch, actor: Actor = Depends(current_actor)):
    r = _own(comment_id, actor)
    if r["user_id"] != actor.user_id:
        raise HTTPException(403, "Only the person who wrote it can edit a comment")
    db.x("UPDATE comments SET body=?, edited_at=? WHERE id=?", (body.body.strip(), time.time(), comment_id))
    return _public(db.q1(f"{SELECT} WHERE c.id=?", (comment_id,)), actor)


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: int, actor: Actor = Depends(current_actor)):
    _own(comment_id, actor, delete=True)
    db.x("DELETE FROM comments WHERE id=?", (comment_id,))
    return {"ok": True}
