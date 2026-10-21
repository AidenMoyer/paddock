"""Live features that go everywhere in the apps: the events socket, calls, and the list of voice rooms."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import access, db
from ..actor import Actor
from ..calls import calls
from ..events import hub
from ..voice import voice
from .deps import current_actor, require_human, websocket_actor

router = APIRouter(prefix="/api")


@router.websocket("/events/ws")
async def events_socket(ws: WebSocket):
    actor = websocket_actor(ws)
    if actor is None or actor.kind not in ("web", "device"):
        await ws.close(code=4401)
        return
    await ws.accept()
    q = hub.attach(actor.user_id)
    rings = [c.public() for c in calls.mine(actor.user_id) if actor.user_id != c.host_id and actor.user_id not in c.declined
             and c.public()["ringing"]]
    await ws.send_text(json.dumps({"type": "hello", "user_id": actor.user_id, "ringing": rings}))

    async def writer():
        while True:
            await ws.send_text(await q.get())

    out = asyncio.create_task(writer())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text") == "ping":
                await ws.send_text('{"type":"pong"}')
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        out.cancel()
        hub.detach(actor.user_id, q)


@router.get("/voice/places")
def voice_places(actor: Actor = Depends(current_actor)):
    """Where this person can talk: their communities' lounges and their calls, with who's in each right now."""
    def roster(room: str) -> list[dict]:
        r = voice.rooms.get(room)
        return r.roster() if r else []

    calls.reap()
    lounges = [{"room": f"community:{c['id']}", "name": c["name"], "community_id": c["id"], "members": roster(f"community:{c['id']}")}
               for c in access.my_communities(actor.user_id, "voice")]
    return {"lounges": lounges, "calls": [c.public() for c in calls.mine(actor.user_id)], "online": sorted(hub.online())}


class CallBody(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=50)


def _callable(actor: Actor, uid: int) -> bool:
    """Admins and people outside any community can call anyone; otherwise only people in a shared community with voice on."""
    if actor.is_admin or not access.communities_of(actor.user_id):
        return True
    return bool(access.shared_communities(actor.user_id, uid, "voice"))


@router.post("/calls")
def start_call(body: CallBody, actor: Actor = Depends(current_actor), _h: Actor = Depends(require_human)):
    people = set()
    for uid in body.user_ids:
        if not db.q1("SELECT 1 FROM users WHERE id=?", (uid,)) or not _callable(actor, uid):
            raise HTTPException(403, "You can only call people in your communities")
        people.add(uid)
    c = calls.start(actor.user_id, actor.username, people)
    db.audit(actor.audit_name, "call.started", call=c.id, people=sorted(people))
    return c.public()


def _call(cid: str, actor: Actor):
    c = calls.calls.get(cid.upper())
    if not c or c.ended or actor.user_id not in c.people():
        raise HTTPException(404, "That call has ended")
    return c


@router.post("/calls/{cid}/decline")
def decline(cid: str, actor: Actor = Depends(current_actor)):
    calls.decline(_call(cid, actor), actor.user_id, actor.username)
    return {"ok": True}


@router.delete("/calls/{cid}")
def end_call(cid: str, actor: Actor = Depends(current_actor)):
    c = _call(cid, actor)
    if actor.user_id != c.host_id and not actor.is_admin:
        raise HTTPException(403, "Only the person who started the call can end it for everyone")
    calls.end(c)
    return {"ok": True}
