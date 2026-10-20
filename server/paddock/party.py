"""Watch parties: several people watching the same thing in sync, wherever they are.

The server keeps one authoritative playback clock per party:

    state = {item_id, playing, position, at, rate}      position (seconds) was true at server time `at`

so every member can compute "where we should be now" = position + (now - at) * rate while playing. Each member
streams the media itself (its own quality, its own connection) and nudges its player toward that target
(see web/static/js/party.js and the Android PartySession). Play / pause / seek / switching titles update the
clock and are broadcast. If anyone is buffering, the party waits for them ("hold") and resumes together.

Parties live in memory: they're for now, not for later. A party is private to the people it was shared with
(or everyone on this server), and voice for a party is the voice room "party:<id>" (see voice.py).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field

MAX_CHAT = 200
HOLD_LIMIT = 15.0          # never wait more than this for a buffering member
START_LEAD = 0.35          # start slightly in the future so everyone begins together
SWITCH_LOAD = 2.5          # extra time to open the next video before a party moves on together
REACTIONS = {"😂", "❤️", "😮", "👏", "😢", "🔥", "👀", "🎉"}


@dataclass
class Member:
    conn: str
    user_id: int
    username: str
    queue: asyncio.Queue
    buffering: bool = False
    joined: float = field(default_factory=time.time)


@dataclass
class Party:
    id: str
    host_id: int
    host_name: str
    item_id: int
    title: str
    everyone: bool = True                       # anyone with an account on this server may join
    invited: set[int] = field(default_factory=set)
    control: str = "everyone"                   # everyone | host
    wait_for_all: bool = True
    created: float = field(default_factory=time.time)
    playing: bool = False
    position: float = 0.0
    at: float = field(default_factory=time.time)
    rate: float = 1.0
    hold: bool = False                          # paused because someone is buffering
    hold_since: float = 0.0
    members: dict[str, Member] = field(default_factory=dict)
    chat: list[dict] = field(default_factory=list)
    ended: bool = False
    empty_since: float = field(default_factory=time.time)

    # -- clock --------------------------------------------------------------------------------------
    def now_position(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        if self.playing and not self.hold and now > self.at:
            return self.position + (now - self.at) * self.rate
        return self.position

    def state(self) -> dict:
        return {"item_id": self.item_id, "title": self.title, "playing": self.playing and not self.hold,
                "intent_playing": self.playing, "position": round(self.position, 3), "at": self.at, "rate": self.rate,
                "hold": self.hold, "waiting_for": [m.username for m in self.members.values() if m.buffering]}

    def public(self) -> dict:
        return {"id": self.id, "host": self.host_name, "host_id": self.host_id, "item_id": self.item_id,
                "title": self.title, "everyone": self.everyone, "control": self.control, "wait_for_all": self.wait_for_all,
                "created": self.created, "members": self.member_list()}

    def member_list(self) -> list[dict]:
        seen: dict[int, dict] = {}
        for m in self.members.values():
            e = seen.setdefault(m.user_id, {"user_id": m.user_id, "username": m.username, "buffering": False,
                                            "host": m.user_id == self.host_id, "devices": 0})
            e["buffering"] = e["buffering"] or m.buffering
            e["devices"] += 1
        return list(seen.values())

    def may_join(self, user_id: int, is_admin: bool) -> bool:
        return self.everyone or user_id == self.host_id or user_id in self.invited or is_admin

    def may_control(self, user_id: int) -> bool:
        return self.control == "everyone" or user_id == self.host_id


class Parties:
    def __init__(self):
        self.parties: dict[str, Party] = {}
        self.lock = asyncio.Lock()

    def new_id(self) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O/1/I: easy to read out loud
        while True:
            pid = "".join(secrets.choice(alphabet) for _ in range(6))
            if pid not in self.parties:
                return pid

    def create(self, host_id: int, host_name: str, item_id: int, title: str, everyone: bool, invited: set[int],
               control: str, wait_for_all: bool, position: float = 0.0) -> Party:
        p = Party(id=self.new_id(), host_id=host_id, host_name=host_name, item_id=item_id, title=title, everyone=everyone,
                  invited=invited, control=control if control in ("everyone", "host") else "everyone",
                  wait_for_all=wait_for_all, position=max(position, 0.0))
        self.parties[p.id] = p
        return p

    def visible_to(self, user_id: int, is_admin: bool) -> list[Party]:
        self.reap()
        return [p for p in self.parties.values() if not p.ended and p.may_join(user_id, is_admin)]

    def reap(self) -> None:
        """Forget parties that have been empty for 10 minutes."""
        now = time.time()
        for pid, p in list(self.parties.items()):
            if p.ended or (not p.members and now - p.empty_since > 600):
                self.parties.pop(pid, None)

    # -- messaging ----------------------------------------------------------------------------------
    @staticmethod
    def send(p: Party, msg: dict, exclude: str | None = None) -> None:
        raw = json.dumps(msg)
        for conn, m in list(p.members.items()):
            if conn != exclude:
                try:
                    m.queue.put_nowait(raw)
                except asyncio.QueueFull:
                    pass   # a member that stopped reading gets dropped by its own socket loop

    def broadcast_state(self, p: Party, by: str | None = None, action: str | None = None) -> None:
        self.send(p, {"type": "state", "state": p.state(), "server": time.time(), "by": by, "action": action})

    def broadcast_members(self, p: Party) -> None:
        self.send(p, {"type": "members", "members": p.member_list()})

    # -- commands from members ----------------------------------------------------------------------
    def handle(self, p: Party, m: Member, msg: dict) -> dict | None:
        """Apply one message from a member. Returns a direct reply for that member, if any."""
        kind = msg.get("type")
        now = time.time()
        if kind == "ping":
            return {"type": "pong", "t0": msg.get("t0"), "server": now}
        if kind == "buffering":
            m.buffering = bool(msg.get("value"))
            self.update_hold(p)
            self.broadcast_members(p)
            return None
        if kind == "chat":
            text = str(msg.get("text", "")).strip()[:500]
            if text:
                entry = {"type": "chat", "user": m.username, "text": text, "ts": now}
                p.chat = (p.chat + [entry])[-MAX_CHAT:]
                self.send(p, entry)
            return None
        if kind == "reaction":
            emoji = msg.get("emoji")
            if emoji in REACTIONS:
                self.send(p, {"type": "reaction", "user": m.username, "emoji": emoji, "ts": now})
            return None
        if kind in ("play", "pause", "seek", "item", "rate"):
            if not p.may_control(m.user_id):
                return {"type": "error", "error": f"Only {p.host_name} controls this party"}
            pos = msg.get("position")
            pos = float(pos) if isinstance(pos, (int, float)) and pos >= 0 else p.now_position(now)
            if kind == "play":
                p.position, p.playing, p.at = pos, True, now + START_LEAD
            elif kind == "pause":
                p.position, p.playing, p.at = pos, False, now
            elif kind == "seek":
                p.position, p.at = pos, now + (START_LEAD if p.playing else 0)
            elif kind == "rate":
                r = msg.get("rate")
                if isinstance(r, (int, float)) and 0.25 <= r <= 3:
                    p.position, p.at, p.rate = p.now_position(now), now, float(r)
            elif kind == "item":
                item = msg.get("item_id")
                title = msg.get("title")
                if not isinstance(item, int):
                    return {"type": "error", "error": "Bad item"}
                p.item_id, p.title = item, str(title or p.title)[:200]
                # "play": keep the party going (autoplay / Next): start once everyone has had time to load
                p.position, p.playing = 0.0, bool(msg.get("play"))
                p.at = now + (START_LEAD + SWITCH_LOAD if p.playing else 0)
                for x in p.members.values():
                    x.buffering = False
                p.hold = False
            self.broadcast_state(p, by=m.username, action=kind)
            return None
        return {"type": "error", "error": "Unknown message"}

    def update_hold(self, p: Party) -> None:
        """Pause for everyone while someone buffers; resume together when they're ready (or after HOLD_LIMIT)."""
        now = time.time()
        waiting = p.wait_for_all and p.playing and any(m.buffering for m in p.members.values())
        if waiting and not p.hold:
            p.position, p.at, p.hold, p.hold_since = p.now_position(now), now, True, now
            self.broadcast_state(p, action="hold")
        elif p.hold and (not waiting or now - p.hold_since > HOLD_LIMIT):
            p.hold, p.at = False, now + START_LEAD
            self.broadcast_state(p, action="resume")

    async def watchdog(self) -> None:
        """Releases holds that last too long (someone's connection gave up)."""
        while True:
            await asyncio.sleep(2)
            for p in list(self.parties.values()):
                if p.hold:
                    self.update_hold(p)
            self.reap()

    # -- membership ---------------------------------------------------------------------------------
    def join(self, p: Party, user_id: int, username: str) -> Member:
        m = Member(conn=secrets.token_hex(6), user_id=user_id, username=username, queue=asyncio.Queue(maxsize=256))
        p.members[m.conn] = m
        self.broadcast_members(p)
        self.send(p, {"type": "notice", "text": f"{username} joined"}, exclude=m.conn)
        return m

    def leave(self, p: Party, m: Member) -> None:
        p.members.pop(m.conn, None)
        if not p.members:
            p.empty_since = time.time()
        self.update_hold(p)
        self.broadcast_members(p)
        self.send(p, {"type": "notice", "text": f"{m.username} left"})

    def end(self, p: Party) -> None:
        p.ended = True
        self.send(p, {"type": "ended"})


parties = Parties()
