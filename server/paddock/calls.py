"""Calls and lounges: voice chat on its own, anywhere in the apps (rooms and audio relay are in voice.py).

- A lounge is a community's always-open voice room ("community:<id>"), for members while that community has
  voice switched on. Drop in and out like a living room.
- A call rings chosen people ("call:<id>"): they get a ring on every device that has Paddock open, and can
  answer or decline. A call ends when the person who started it ends it, or a minute after everyone left.
- Watch parties have their own room ("party:<id>", see api/party.py).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from . import access, db
from .events import hub
from .voice import register_scope, voice

RING_S = 45                 # stop ringing after this long
EMPTY_S = 60                # end a call nobody is in any more


@dataclass
class Call:
    id: str
    host_id: int
    host_name: str
    invited: set[int]
    created: float = field(default_factory=time.time)
    declined: set[int] = field(default_factory=set)
    ended: bool = False
    empty_since: float | None = None

    @property
    def room(self) -> str:
        return f"call:{self.id}"

    def people(self) -> set[int]:
        return self.invited | {self.host_id}

    def public(self) -> dict:
        names = {r["id"]: r["display_name"] or r["username"] for r in db.q(
            f"SELECT id, username, display_name FROM users WHERE id IN ({','.join('?' * len(self.people()))})", list(self.people()))}
        r = voice.rooms.get(self.room)
        return {"id": self.id, "room": self.room, "host_id": self.host_id, "host": self.host_name, "created": self.created,
                "people": [{"user_id": u, "name": names.get(u, "?"), "declined": u in self.declined} for u in sorted(self.people())],
                "in_call": r.roster() if r else [], "ringing": time.time() - self.created < RING_S}


class Calls:
    def __init__(self):
        self.calls: dict[str, Call] = {}

    def start(self, host_id: int, host_name: str, invited: set[int]) -> Call:
        c = Call(id=secrets.token_hex(4).upper(), host_id=host_id, host_name=host_name, invited=invited - {host_id})
        self.calls[c.id] = c
        hub.send(c.invited, {"type": "ring", "call": c.public()})
        return c

    def mine(self, user_id: int) -> list[Call]:
        return [c for c in self.calls.values() if not c.ended and user_id in c.people()]

    def decline(self, c: Call, user_id: int, name: str) -> None:
        c.declined.add(user_id)
        hub.send(c.people() - {user_id}, {"type": "declined", "call_id": c.id, "user_id": user_id, "name": name})
        hub.send([user_id], {"type": "ring_stop", "call_id": c.id})

    def end(self, c: Call) -> None:
        if c.ended:
            return
        c.ended = True
        hub.send(c.people(), {"type": "call_ended", "call_id": c.id})
        self.calls.pop(c.id, None)

    def reap(self) -> None:
        now = time.time()
        for c in list(self.calls.values()):
            r = voice.rooms.get(c.room)
            if r and r.members:
                c.empty_since = None
            elif now - c.created > RING_S:
                c.empty_since = c.empty_since or now
                if now - c.empty_since > EMPTY_S:
                    self.end(c)


calls = Calls()


def _call_scope(actor, ref: str) -> bool:
    c = calls.calls.get(ref)
    return bool(c and not c.ended and actor.user_id in c.people())


def _community_scope(actor, ref: str) -> bool:
    try:
        cid = int(ref)
    except ValueError:
        return False
    row = db.q1("SELECT * FROM communities WHERE id=?", (cid,))
    return bool(row and access.community_settings(row).get("voice", True) and access.is_member(actor.user_id, cid))


register_scope("call", _call_scope)
register_scope("community", _community_scope)
