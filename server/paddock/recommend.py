"""What plays next: the rest of the show or album first, then a random pick of things like it.

"Like it" stays within the same kind of collection, so a film never rolls into a camera-roll clip. Everything is
worked out from what's already on the server, with no outside service:

- what it is: shared genres and tags (never technical ones like "1080p"), the artist, director, cast or film series;
- where it is: files kept in the same folders usually belong together, which also covers libraries with no metadata;
- what it's called: "Iron Forge 3" follows "Iron Forge 2" (videos only, since songs share words by chance);
- when it's from: the same era, a similar length, and for memories the same days and places;
- what people did: things watched in the same sitting, and what this person has just played, which sinks for a while.

Then two rules keep it pleasant to watch. Another show is only ever entered at the next episode to watch, never in
the middle of a season, and a few songs by one artist give way to others. A little randomness on top means the list
is different every time without becoming arbitrary.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from collections import Counter

from . import access, catalog, db

VIDEO_KINDS = ["video", "red_clip", "braw_clip"]
# library kinds that belong together when choosing what's similar
FAMILIES = [{"movies", "shows"}, {"home_videos", "photos", "mixed"}, {"red"}, {"music"}, {"engineering"}]

TECHNICAL = re.compile(r"^(?:\d{3,4}p|\dk|sd|hd|uhd|hdr\d*|dv|raw|r3d|braw|screenshot)$", re.I)
SEPARATORS = re.compile(r"\s*[;,/|]\s*")
FEATURING = re.compile(r"\s*(?:[;,/|]|\bfeat\.?|\bft\.?|\bfeaturing\b)\s*", re.I)
NOT_IN_A_NAME = {"the", "a", "an", "of", "and", "in", "on", "to", "at", "part", "episode", "ep", "vol", "volume", "disc", "cd"}

SITTING = 6 * 3600          # played this close together counts as watched together
TEMPERATURE = 1.0           # higher means more surprises, lower means closer to a strict ranking
VARIETY = 1.5               # how quickly a run of one artist or show gives way to something else


def _family(lib_kind: str) -> set[str]:
    return next((f for f in FAMILIES if lib_kind in f), {lib_kind})


def _near(a: dict, b: dict) -> bool:
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return False
    return abs(a["lat"] - b["lat"]) < 0.02 and abs(a["lon"] - b["lon"]) < 0.02    # roughly 2 km


def _lower_set(values, pattern: re.Pattern = SEPARATORS) -> set[str]:
    out: set[str] = set()
    for v in values:
        if isinstance(v, (list, tuple)):
            out |= _lower_set(v, pattern)
        elif v:
            out |= {x.lower() for x in pattern.split(str(v)) if x.strip()}
    return out


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in NOT_IN_A_NAME and not w.isdigit()}


def _year(meta: dict) -> int | None:
    y = meta.get("year") or meta.get("date") or (meta.get("tags") or {}).get("date")
    try:
        return int(str(y)[:4]) if y else None
    except ValueError:
        return None


def _facts(row: dict, tags: list[str]) -> dict:
    """What can be compared about one item, read once."""
    meta = json.loads(row.get("meta") or "{}")
    described = [t for t in tags if not TECHNICAL.match(t) and t != meta.get("model")]
    return {
        "labels": _lower_set([*described, meta.get("genre"), (meta.get("tags") or {}).get("genre")]),
        "artists": _lower_set([meta.get("albumartist"), meta.get("artist"), (meta.get("tags") or {}).get("artist")], FEATURING),
        "people": _lower_set([meta.get("directors"), meta.get("people")]),
        "series": str(meta.get("set") or "").lower(),
        "year": _year(meta),
        "folders": row["rel_path"].split("/")[:-1],
        "words": _words(row["title"]),
    }


def _candidates(item: dict, mine: dict, kinds: list[str], family: set[str], allowed: set[int] | None, together: Counter) -> list[dict]:
    """A random sample for surprises, plus everything obviously related, which a sample of a big library would miss."""
    frag, ids = access.clause(allowed)
    base = (f"FROM items i JOIN libraries l ON l.id=i.library_id WHERE i.trashed_at IS NULL "
            f"AND i.kind IN ({','.join('?' * len(kinds))}) AND l.kind IN ({','.join('?' * len(family))}){frag}")
    args = (*kinds, *sorted(family), *ids)
    found = {r["id"] for r in db.q(f"SELECT i.id {base} ORDER BY RANDOM() LIMIT 600", args)}

    def related(condition: str, *values) -> None:
        found.update(r["id"] for r in db.q(f"SELECT i.id {base} AND {condition} LIMIT 200", (*args, *values)))

    for artist in list(mine["artists"])[:3]:
        related("(lower(json_extract(i.meta,'$.artist')) LIKE ? ESCAPE '\\' OR lower(json_extract(i.meta,'$.albumartist')) LIKE ? ESCAPE '\\')",
                *[f"%{_escape(artist)}%"] * 2)
    for label in list(mine["labels"])[:5]:
        related("i.id IN (SELECT item_id FROM tags WHERE lower(tag) LIKE ? ESCAPE '\\')", f"%{_escape(label)}%")
    if mine["folders"]:
        related("i.rel_path LIKE ? ESCAPE '\\'", f"{_escape(mine['folders'][0])}/%")
    if together:
        related(f"i.id IN ({','.join('?' * len(together))})", *together)
    if not found:
        return []
    return db.q(f"SELECT {catalog.ITEM_COLS} {base} AND i.id IN ({','.join('?' * len(found))})", (*args, *found))


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _next_of_each_show(pool: list[dict], user_id: int, allowed: set[int] | None) -> list[dict]:
    """Another show is only ever entered at the next episode this person hasn't seen, so a big show doesn't swamp the
    list either: it counts as one choice, not a hundred."""
    shows = {r["group_key"] for r in pool if (r["group_key"] or "").startswith("show:")}
    if not shows:
        return pool
    frag, ids = access.clause(allowed)
    episodes = db.q(f"SELECT {catalog.ITEM_COLS}, COALESCE(p.completed, 0) AS seen FROM items i JOIN libraries l ON l.id=i.library_id "
                    f"LEFT JOIN progress p ON p.item_id=i.id AND p.user_id=? "
                    f"WHERE i.trashed_at IS NULL AND i.group_key IN ({','.join('?' * len(shows))}){frag} ORDER BY i.group_key, i.sort_key",
                    (user_id, *shows, *ids))
    chosen: dict[str, dict] = {}
    for e in episodes:
        key = e["group_key"]
        if key not in chosen or (chosen[key]["seen"] and not e["seen"]):
            chosen[key] = e
    return [r for r in pool if r["group_key"] not in shows] + list(chosen.values())


def similar(item: dict, user_id: int, allowed: set[int] | None, exclude: set[int], limit: int = 30,
            rng: random.Random | None = None) -> list[dict]:
    rng = rng or random.Random()
    lib = db.q1("SELECT kind FROM libraries WHERE id=?", (item["library_id"],))
    family = _family(lib["kind"] if lib else "mixed")
    audio = item["kind"] == "audio"
    kinds = ["audio"] if audio else VIDEO_KINDS
    memories = family == {"home_videos", "photos", "mixed"}
    mine = _facts(item, catalog.tags_for([item["id"]]).get(item["id"], []))
    together = Counter({r["item_id"]: r["c"] for r in db.q(
        "SELECT b.item_id, COUNT(*) AS c FROM progress a JOIN progress b ON b.user_id=a.user_id AND b.item_id!=a.item_id "
        "AND ABS(b.updated_at - a.updated_at) < ? WHERE a.item_id=? GROUP BY b.item_id ORDER BY c DESC LIMIT 100", (SITTING, item["id"]))})

    pool = _candidates(item, mine, kinds, family, allowed, together)
    pool = [r for r in _next_of_each_show(pool, user_id, allowed) if r["id"] not in exclude]
    if not pool:
        return []
    tags = catalog.tags_for([r["id"] for r in pool])
    facts = {r["id"]: _facts(r, tags.get(r["id"], [])) for r in pool}
    played = {r["item_id"]: r for r in db.q(
        f"SELECT item_id, completed, position, updated_at FROM progress WHERE user_id=? AND item_id IN ({','.join('?' * len(pool))})",
        (user_id, *[r["id"] for r in pool]))}
    now = time.time()

    def score(r: dict) -> float:
        f = facts[r["id"]]
        s = 2.5 * min(len(mine["labels"] & f["labels"]), 3)
        s += 1.5 if r["library_id"] == item["library_id"] else 0
        common = 0
        for a, b in zip(mine["folders"], f["folders"]):
            if a != b:
                break
            common += 1
        s += min(3.0, 1.5 * common)
        if mine["year"] and f["year"]:
            s += max(0.0, 2.0 - abs(mine["year"] - f["year"]) / 5)
        if audio:
            s += 4.0 if mine["artists"] & f["artists"] else 0
        else:
            if mine["words"] and f["words"]:
                overlap = len(mine["words"] & f["words"]) / min(len(mine["words"]), len(f["words"]))
                s += 4.0 * overlap ** 2
                if overlap > 0.5 and mine["year"] and f["year"] and f["year"] > mine["year"]:
                    s += 1.0            # the sequel before the prequel
            if mine["series"] and mine["series"] == f["series"]:
                s += 5.0
            s += min(len(mine["people"] & f["people"]) * 1.5, 4.5)
            if item.get("duration") and r.get("duration"):
                s += 0.75 if min(item["duration"], r["duration"]) / max(item["duration"], r["duration"]) > 0.6 else 0
        if memories and item.get("taken_at") and r.get("taken_at"):
            days = abs(item["taken_at"] - r["taken_at"]) / 86400
            s += 3.0 if days < 3 else 1.5 if days < 30 else 0
            s += 2.0 if _near(item, r) else 0
        s += min(together[r["id"]] * 1.25, 3.75)
        seen = played.get(r["id"])
        if seen:
            hours = (now - seen["updated_at"]) / 3600
            if hours < 2:
                s -= 8.0                # just played: not again so soon
            elif hours < 24:
                s -= 4.0
            elif seen["completed"]:
                s -= 0.5 if audio else 2.0     # a favourite song can come back; a film you've seen less so
            elif seen["position"] and not audio:
                s += 1.0                # started and not finished
        return s

    # Gumbel noise turns scores into a weighted random order: alike things first, but never the same list twice
    keyed = {r["id"]: score(r) / TEMPERATURE - math.log(-math.log(rng.random() or 1e-12)) for r in pool}

    def run_of(r: dict) -> str:
        return sorted(facts[r["id"]]["artists"])[0] if audio and facts[r["id"]]["artists"] else (r["group_key"] or "")

    runs: Counter = Counter()
    chosen: list[dict] = []
    rest = list(pool)
    while rest and len(chosen) < limit:
        best = max(rest, key=lambda r: keyed[r["id"]] - VARIETY * runs[run_of(r)])
        rest.remove(best)
        runs[run_of(best)] += 1
        chosen.append(best)
    return catalog.cards(chosen)


def up_next(item: dict, user_id: int, allowed: set[int] | None, limit: int = 30, rng: random.Random | None = None) -> list[dict]:
    kinds = ["audio"] if item["kind"] == "audio" else VIDEO_KINDS
    out: list[dict] = []
    seen = {item["id"]}
    if item.get("group_key") and not item["group_key"].startswith(("day:", "folder:")):
        sibs = [c for c in catalog.search(group=item["group_key"], sort="group", limit=300, allowed=allowed) if c["kind"] in kinds]
        idx = next((i for i, c in enumerate(sibs) if c["id"] == item["id"]), -1)
        out = sibs[idx + 1:]              # the rest of the show/album, in order
        seen |= {c["id"] for c in sibs}   # and when it ends, on to something else rather than round again
    out += similar(item, user_id, allowed, seen, max(limit - len(out), 0), rng)
    return out[:limit]
