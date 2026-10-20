"""Voice rooms: live talk between people, relayed by Paddock itself (no outside service, no extra ports).

Clients capture their microphone with echo cancellation, encode 20 ms frames (Opus, or plain 16 kHz PCM where
an Opus encoder isn't available) and send them as binary WebSocket messages over the same HTTPS connection
the app already uses, at home or through the app-only tunnel. The server never decodes audio: it forwards
each frame to everyone else in the room, tagged with who said it. Each listener decodes and mixes the
speakers itself (web/static/js/voice.js, Android VoiceSession).

Rooms are generic. A feature owns a scope and decides who may enter, e.g. watch parties register "party" so
that room "party:ABC123" is open to that party's members. Other features (watching TVs around the house,
working on code together) can register their own scopes later.

Binary frames
    client → server:   [codec u8][seq u16 BE][payload]
    server → clients:  [speaker u16 BE][codec u8][seq u16 BE][payload]
    codec: 1 = Opus 48 kHz mono, 2 = PCM s16le 16 kHz mono
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

MAX_FRAME = 4000            # bytes of audio per 20 ms frame (Opus ~60-120, PCM 640)
MAX_FPS = 120               # frames per second per speaker before we start dropping
CODECS = {1: "opus", 2: "pcm16"}

Scope = Callable[[object, str], bool]     # (actor, ref) -> may this person enter room "<scope>:<ref>"?
_scopes: dict[str, Scope] = {}


def register_scope(name: str, check: Scope) -> None:
    _scopes[name] = check


def may_enter(actor, room: str) -> bool:
    scope, _, ref = room.partition(":")
    check = _scopes.get(scope)
    return bool(check and ref and check(actor, ref))


@dataclass
class Speaker:
    idx: int
    user_id: int
    username: str
    queue: asyncio.Queue
    muted: bool = False
    deafened: bool = False
    codec: str = "opus"
    speaking: bool = False
    window: list = field(default_factory=lambda: [0.0, 0])    # [window start, frames in window]

    def public(self) -> dict:
        return {"id": self.idx, "user_id": self.user_id, "username": self.username, "muted": self.muted,
                "deafened": self.deafened, "speaking": self.speaking and not self.muted, "codec": self.codec}


@dataclass
class Room:
    id: str
    members: dict[int, Speaker] = field(default_factory=dict)
    next_idx: int = 1

    def roster(self) -> list[dict]:
        return [m.public() for m in self.members.values()]


class Voice:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    def room(self, room_id: str) -> Room:
        return self.rooms.setdefault(room_id, Room(room_id))

    def join(self, room_id: str, user_id: int, username: str) -> tuple[Room, Speaker]:
        r = self.room(room_id)
        idx = r.next_idx
        r.next_idx = r.next_idx % 65000 + 1
        s = Speaker(idx=idx, user_id=user_id, username=username, queue=asyncio.Queue(maxsize=200))
        r.members[idx] = s
        self.roster(r)
        return r, s

    def leave(self, r: Room, s: Speaker) -> None:
        r.members.pop(s.idx, None)
        if r.members:
            self.roster(r)
        else:
            self.rooms.pop(r.id, None)

    @staticmethod
    def _put(s: Speaker, item) -> None:
        try:
            s.queue.put_nowait(item)
        except asyncio.QueueFull:
            # a listener that can't keep up loses old audio rather than falling behind for ever
            try:
                s.queue.get_nowait()
                s.queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def roster(self, r: Room) -> None:
        msg = json.dumps({"type": "members", "members": r.roster()})
        for m in r.members.values():
            self._put(m, msg)

    def text(self, r: Room, s: Speaker, msg: dict) -> dict | None:
        kind = msg.get("type")
        if kind == "state":
            if "muted" in msg:
                s.muted = bool(msg["muted"])
            if "deafened" in msg:
                s.deafened = bool(msg["deafened"])
                if s.deafened:
                    s.muted = True
            if msg.get("codec") in CODECS.values():
                s.codec = msg["codec"]
            self.roster(r)
        elif kind == "speaking":
            value = bool(msg.get("value"))
            if value != s.speaking:
                s.speaking = value
                note = json.dumps({"type": "speaking", "id": s.idx, "value": value and not s.muted})
                for m in r.members.values():
                    self._put(m, note)
        elif kind == "ping":
            return {"type": "pong", "t0": msg.get("t0"), "server": time.time()}
        return None

    def audio(self, r: Room, s: Speaker, frame: bytes) -> bool:
        """Relay one frame from s to everyone else. Returns False if the frame was dropped."""
        if s.muted or len(frame) < 4 or len(frame) > MAX_FRAME + 3 or frame[0] not in CODECS:
            return False
        now = time.monotonic()
        start, count = s.window
        if now - start >= 1.0:
            s.window = [now, 1]
        elif count >= MAX_FPS:
            return False
        else:
            s.window[1] = count + 1
        out = struct.pack(">H", s.idx) + frame
        for idx, m in r.members.items():
            if idx != s.idx and not m.deafened:
                self._put(m, out)
        return True


voice = Voice()
