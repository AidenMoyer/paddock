"""Watch parties (synced playback) — REST to create/list/end, a WebSocket per member for the live clock."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import db
from ..actor import Actor
from ..party import parties
from ..voice import register_scope
from .deps import current_actor, require_human, websocket_actor

router = APIRouter(prefix="/api")


def _party_voice_scope(actor, ref: str) -> bool:
    p = parties.parties.get(ref)
    return bool(p and not p.ended and p.may_join(actor.user_id, actor.is_admin))


register_scope("party", _party_voice_scope)


def _item_for(user_id: int, item_id: int) -> dict | None:
    """An item this person may put on screen in a party (they must be able to open it themselves)."""
    from .. import access
    from ..actor import for_user

    u = db.q1("SELECT * FROM users WHERE id=?", (user_id,))
    item = db.q1("SELECT * FROM items WHERE id=? AND trashed_at IS NULL", (item_id,))
    if not u or not item or item["kind"] not in ("video", "red_clip", "braw_clip", "audio"):
        return None
    return item if access.can_read_library(for_user(u), item["library_id"]) else None


parties.item_check = _item_for

WARM_SEGMENTS = 3            # about the first 18 seconds


def _warm(item_id: int) -> None:
    """Encode the start of a video before the party gets to it, so everyone (near or far) starts together
    instead of all waiting on the transcoder. Runs in the background; the segments land in the shared cache."""
    from .. import jobs, transcode
    from .media import _source_for

    item = db.q1("SELECT * FROM items WHERE id=? AND trashed_at IS NULL", (item_id,))
    if not item or item["kind"] not in ("video", "red_clip"):
        return
    meta = json.loads(item["meta"] or "{}")

    def run(ctx):
        try:
            src = _source_for(item, meta)
        except Exception:  # noqa: BLE001 - e.g. a RED clip without a proxy: nothing to warm
            return "skipped"
        rungs = [p for p in transcode.available_profiles(meta) if p in ("1080p", "720p", "480p")][:2]
        for p in rungs:
            for seg in range(WARM_SEGMENTS):
                transcode.segments.segment(item, meta, src, p, seg)
        return f"warmed {', '.join(rungs)}"

    jobs.submit("party-warm", str(item_id), run)


parties.on_upcoming = _warm


class NewParty(BaseModel):
    item_id: int
    everyone: bool = True
    invite: list[str] = Field(default_factory=list, max_length=50)
    control: str = Field(default="everyone", pattern=r"^(everyone|host)$")
    wait_for_all: bool = True
    position: float = Field(default=0, ge=0)
    community_id: int | None = None              # open to everyone in this community


def _get(pid: str, actor: Actor):
    p = parties.parties.get(pid.upper())
    if not p or p.ended or not p.may_join(actor.user_id, actor.is_admin):
        raise HTTPException(404, "That watch party has ended or isn't shared with you")
    return p


@router.post("/parties")
def create_party(body: NewParty, actor: Actor = Depends(current_actor), _h: Actor = Depends(require_human)):
    from .. import access

    item = db.q1("SELECT id, title, library_id FROM items WHERE id=? AND trashed_at IS NULL", (body.item_id,))
    if not item or not access.can_read_item(actor, item):
        raise HTTPException(404, "Item not found")
    invited = set()
    for name in body.invite:
        u = db.q1("SELECT id FROM users WHERE username=?", (name.strip(),))
        if not u:
            raise HTTPException(400, f"No account called {name}")
        invited.add(u["id"])
    community = None
    if body.community_id is not None:
        community = next((c for c in access.my_communities(actor.user_id, "parties") if c["id"] == body.community_id), None)
        if not community and not actor.is_admin:
            raise HTTPException(403, "You can't start a watch party in that community")
        community = community or db.q1("SELECT id, name FROM communities WHERE id=?", (body.community_id,))
        if not community:
            raise HTTPException(404, "No such community")
    p = parties.create(actor.user_id, actor.username, item["id"], item["title"], body.everyone and not community, invited,
                       body.control, body.wait_for_all, body.position, community["id"] if community else None,
                       community["name"] if community else "")
    # tell the people who can join (the community's members, or those invited) that it's on
    from ..events import hub

    audience = set(invited)
    if community:
        audience |= {r["user_id"] for r in db.q("SELECT user_id FROM community_members WHERE community_id=?", (community["id"],))}
    hub.send(audience - {actor.user_id}, {"type": "party", "party": p.public()})
    db.audit(actor.audit_name, "party.created", party=p.id, item=item["id"], everyone=body.everyone)
    return p.public() | {"state": p.state()}


@router.get("/people")
def people(actor: Actor = Depends(current_actor)):
    """Who you can invite or call (names only): everyone for admins and people outside any community,
    otherwise the people in your communities."""
    from .. import access

    mine = access.communities_of(actor.user_id)
    if actor.is_admin or not mine:
        rows = db.q("SELECT id, username, display_name FROM users ORDER BY username COLLATE NOCASE")
    else:
        rows = db.q(f"SELECT DISTINCT u.id, u.username, u.display_name FROM users u JOIN community_members m ON m.user_id=u.id "
                    f"WHERE m.community_id IN ({','.join('?' * len(mine))}) ORDER BY u.username COLLATE NOCASE", mine)
    return [{"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"]}
            for u in rows if u["id"] != actor.user_id]


@router.get("/parties")
def list_parties(actor: Actor = Depends(current_actor)):
    return [p.public() | {"state": p.state()} for p in parties.visible_to(actor.user_id, actor.is_admin)]


@router.get("/parties/{pid}")
def get_party(pid: str, actor: Actor = Depends(current_actor)):
    p = _get(pid, actor)
    return p.public() | {"state": p.state(), "server": time.time()}


@router.delete("/parties/{pid}")
def end_party(pid: str, actor: Actor = Depends(current_actor)):
    p = _get(pid, actor)
    if actor.user_id != p.host_id and not actor.is_admin:
        raise HTTPException(403, "Only the host can end the party")
    parties.end(p)
    db.audit(actor.audit_name, "party.ended", party=p.id)
    return {"ended": p.id}


@router.websocket("/parties/{pid}/ws")
async def party_socket(ws: WebSocket, pid: str):
    actor = websocket_actor(ws)
    if actor is None:
        await ws.close(code=4401)
        return
    p = parties.parties.get(pid.upper())
    if not p or p.ended or not p.may_join(actor.user_id, actor.is_admin):
        await ws.close(code=4404)
        return
    await ws.accept()
    m = parties.join(p, actor.user_id, actor.username)
    await ws.send_text(json.dumps({
        "type": "hello", "you": {"user_id": actor.user_id, "username": actor.username, "conn": m.conn},
        "party": p.public(), "state": p.state(), "server": time.time(), "chat": p.chat[-50:],
        "can_control": p.may_control(actor.user_id),
    }))

    async def writer():
        while True:
            await ws.send_text(await m.queue.get())

    out = asyncio.create_task(writer())
    try:
        while not p.ended:
            raw = await ws.receive_text()
            if len(raw) > 4000:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                reply = parties.handle(p, m, msg)
                if reply:
                    m.queue.put_nowait(json.dumps(reply))
        await asyncio.sleep(0.2)   # let "ended" reach this member
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        out.cancel()
        parties.leave(p, m)
