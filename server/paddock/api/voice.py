"""Voice rooms over a WebSocket: JSON for state, binary for 20 ms audio frames (see paddock/voice.py)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..voice import may_enter, voice
from .deps import websocket_actor

router = APIRouter(prefix="/api")


@router.websocket("/voice/{room}/ws")
async def voice_socket(ws: WebSocket, room: str):
    actor = websocket_actor(ws)
    if actor is None:
        await ws.close(code=4401)
        return
    if len(room) > 80 or not may_enter(actor, room):
        await ws.close(code=4403)
        return
    await ws.accept()
    r, s = voice.join(room, actor.user_id, actor.username)
    await ws.send_text(json.dumps({"type": "hello", "you": s.idx, "room": room, "members": r.roster()}))

    async def writer():
        while True:
            item = await s.queue.get()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_text(item)

    out = asyncio.create_task(writer())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                voice.audio(r, s, msg["bytes"])
            elif msg.get("text"):
                if len(msg["text"]) > 2000:
                    continue
                try:
                    data = json.loads(msg["text"])
                except ValueError:
                    continue
                if isinstance(data, dict):
                    reply = voice.text(r, s, data)
                    if reply:
                        voice._put(s, json.dumps(reply))
                    if not may_enter(actor, room):     # e.g. the party ended
                        break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        out.cancel()
        voice.leave(r, s)
