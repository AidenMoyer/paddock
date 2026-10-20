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


class NewParty(BaseModel):
    item_id: int
    everyone: bool = True
    invite: list[str] = Field(default_factory=list, max_length=50)
    control: str = Field(default="everyone", pattern=r"^(everyone|host)$")
    wait_for_all: bool = True
    position: float = Field(default=0, ge=0)


def _get(pid: str, actor: Actor):
    p = parties.parties.get(pid.upper())
    if not p or p.ended or not p.may_join(actor.user_id, actor.is_admin):
        raise HTTPException(404, "That watch party has ended or isn't shared with you")
    return p


@router.post("/parties")
def create_party(body: NewParty, actor: Actor = Depends(current_actor), _h: Actor = Depends(require_human)):
    item = db.q1("SELECT id, title FROM items WHERE id=? AND trashed_at IS NULL", (body.item_id,))
    if not item:
        raise HTTPException(404, "Item not found")
    invited = set()
    for name in body.invite:
        u = db.q1("SELECT id FROM users WHERE username=?", (name.strip(),))
        if not u:
            raise HTTPException(400, f"No account called {name}")
        invited.add(u["id"])
    p = parties.create(actor.user_id, actor.username, item["id"], item["title"], body.everyone, invited, body.control,
                       body.wait_for_all, body.position)
    db.audit(actor.audit_name, "party.created", party=p.id, item=item["id"], everyone=body.everyone)
    return p.public() | {"state": p.state()}


@router.get("/people")
def people(actor: Actor = Depends(current_actor)):
    """Who you can invite (names only)."""
    return [{"username": u["username"]} for u in db.q("SELECT username FROM users ORDER BY username COLLATE NOCASE")]


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
