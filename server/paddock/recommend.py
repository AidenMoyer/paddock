"""What plays next: the rest of the show or album first, then a random pick of things like it.

"Like it" stays within the same kind of collection — movies and shows lead to movies and shows, home videos
to other memories, songs to songs — so a film never rolls into a camera-roll clip. Within that, items that
share tags, the artist or genre, the era, or (for memories) the same days and places score higher, and a
little randomness on top means the list is different every time without becoming arbitrary.
"""

from __future__ import annotations

import json
import math
import random

from . import access, catalog, db

VIDEO_KINDS = ["video", "red_clip", "braw_clip"]
# library kinds that belong together when choosing what's similar
FAMILIES = [{"movies", "shows"}, {"home_videos", "photos", "mixed"}, {"red"}, {"music"}, {"engineering"}]


def _family(lib_kind: str) -> set[str]:
    return next((f for f in FAMILIES if lib_kind in f), {lib_kind})


def _year(row: dict) -> int | None:
    meta = json.loads(row.get("meta") or "{}")
    y = meta.get("year") or (meta.get("tags") or {}).get("date")
    try:
        return int(str(y)[:4]) if y else None
    except ValueError:
        return None


def _genre(row: dict) -> str | None:
    meta = json.loads(row.get("meta") or "{}")
    g = meta.get("genre") or (meta.get("tags") or {}).get("genre")
    return str(g).lower() if g else None


def _artist(row: dict) -> str | None:
    meta = json.loads(row.get("meta") or "{}")
    a = meta.get("artist") or (meta.get("tags") or {}).get("artist")
    return str(a).lower() if a else None


def _near(a: dict, b: dict) -> bool:
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return False
    return abs(a["lat"] - b["lat"]) < 0.02 and abs(a["lon"] - b["lon"]) < 0.02    # roughly 2 km


def similar(item: dict, user_id: int, allowed: set[int] | None, exclude: set[int], limit: int = 30,
            rng: random.Random | None = None) -> list[dict]:
    rng = rng or random.Random()
    lib = db.q1("SELECT kind FROM libraries WHERE id=?", (item["library_id"],))
    family = _family(lib["kind"] if lib else "mixed")
    kinds = ["audio"] if item["kind"] == "audio" else VIDEO_KINDS
    frag, ids = access.clause(allowed)
    pool = db.q(f"SELECT {catalog.ITEM_COLS} FROM items i JOIN libraries l ON l.id=i.library_id "
                f"WHERE i.trashed_at IS NULL AND i.kind IN ({','.join('?' * len(kinds))}) "
                f"AND l.kind IN ({','.join('?' * len(family))}){frag} ORDER BY RANDOM() LIMIT 600",
                (*kinds, *sorted(family), *ids))
    pool = [r for r in pool if r["id"] not in exclude]
    if not pool:
        return []
    tags = catalog.tags_for([item["id"]] + [r["id"] for r in pool])
    mine = set(tags.get(item["id"], []))
    done = {r["item_id"] for r in db.q(
        f"SELECT item_id FROM progress WHERE user_id=? AND completed=1 AND item_id IN ({','.join('?' * len(pool))})",
        (user_id, *[r["id"] for r in pool]))}
    year, genre, artist = _year(item), _genre(item), _artist(item)
    memories = family == {"home_videos", "photos", "mixed"}

    def score(r: dict) -> float:
        s = 2.5 * len(mine & set(tags.get(r["id"], [])))
        s += 1.0 if r["library_id"] == item["library_id"] else 0
        y = _year(r)
        if year and y:
            s += max(0.0, 2.0 - abs(year - y) / 5)
        if genre and _genre(r) == genre:
            s += 2.0
        if artist and _artist(r) == artist:
            s += 4.0
        if memories and item.get("taken_at") and r.get("taken_at"):
            days = abs(item["taken_at"] - r["taken_at"]) / 86400
            s += 3.0 if days < 3 else 1.5 if days < 30 else 0
            s += 2.0 if _near(item, r) else 0
        if r["id"] in done:
            s -= 3.0                                     # already watched: still possible, just less likely
        return s

    # Gumbel noise turns scores into a weighted random order: alike things first, but never the same list twice
    keyed = sorted(pool, key=lambda r: score(r) / 1.5 - math.log(-math.log(rng.random() or 1e-12)), reverse=True)
    return catalog.cards(keyed[:limit])


def up_next(item: dict, user_id: int, allowed: set[int] | None, limit: int = 30) -> list[dict]:
    kinds = ["audio"] if item["kind"] == "audio" else VIDEO_KINDS
    out: list[dict] = []
    seen = {item["id"]}
    if item.get("group_key") and not item["group_key"].startswith(("day:", "folder:")):
        sibs = [c for c in catalog.search(group=item["group_key"], sort="group", limit=300, allowed=allowed) if c["kind"] in kinds]
        idx = next((i for i, c in enumerate(sibs) if c["id"] == item["id"]), -1)
        for c in sibs[idx + 1:]:          # the rest of the show/album, in order
            seen.add(c["id"])
            out.append(c)
    out += similar(item, user_id, allowed, seen, max(limit - len(out), 0))
    return out[:limit]
