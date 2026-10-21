"""Live events for each signed-in person, over one WebSocket per app (/api/events/ws).

Phones, computers and browsers keep this open while the app is running and hear about things meant for them:
a call ringing, someone declining or ending a call, a new comment on something they can see, an invitation
to a watch party. Anything can publish from any thread; delivery is best-effort (the apps also fetch state
when they open a screen), and a slow client just misses old events instead of holding the server up.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Iterable

from . import access, db
from .actor import for_user


class Hub:
    def __init__(self):
        self.conns: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None

    def attach(self, user_id: int) -> asyncio.Queue:
        self.loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.conns[user_id].add(q)
        return q

    def detach(self, user_id: int, q: asyncio.Queue) -> None:
        self.conns[user_id].discard(q)
        if not self.conns[user_id]:
            self.conns.pop(user_id, None)

    def online(self) -> set[int]:
        return set(self.conns)

    def send(self, user_ids: Iterable[int], msg: dict) -> None:
        text = json.dumps(msg)
        for uid in set(user_ids):
            for q in list(self.conns.get(uid, ())):
                self._put(q, text)

    def _put(self, q: asyncio.Queue, text: str) -> None:
        def put():
            if q.full():
                try:
                    q.get_nowait()       # drop the oldest
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(text)

        loop = self.loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            put()
        else:
            loop.call_soon_threadsafe(put)


hub = Hub()


def _actor(uid: int):
    u = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    return for_user(u) if u else None


def publish_comment(comment: dict) -> None:
    """Tell everyone online who may see this comment (and the item it's on)."""
    item = db.q1("SELECT * FROM items WHERE id=?", (comment["item_id"],))
    if not item:
        return
    for uid in hub.online():
        if uid == comment["user_id"]:
            continue
        a = _actor(uid)
        if not a or not access.can_read_item(a, item):
            continue
        cid = comment["community_id"]
        if cid is None:
            if not db.get_setting("comments_everyone", True):
                continue
        elif not a.is_admin and cid not in [c["id"] for c in access.my_communities(uid, "comments")]:
            continue
        hub.send([uid], {"type": "comment", "comment": comment | {"mine": False}, "title": item["title"]})
