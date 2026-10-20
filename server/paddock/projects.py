"""Projects: a build, a trip, a race weekend — media, CAD, notes, places and a video plan in one place."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import datetime

from . import catalog, db

ROLES = ("footage", "broll", "photo", "audio", "music", "cad", "print", "reference")
STATUSES = ("idea", "active", "filming", "editing", "done")


def create(name: str, description: str = "", goal: str = "", location_name: str | None = None,
           lat: float | None = None, lon: float | None = None, status: str = "active") -> int:
    now = time.time()
    return db.x("INSERT INTO projects(name, description, goal, status, location_name, lat, lon, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (name.strip()[:200], description, goal, status if status in STATUSES else "active",
                 location_name, lat, lon, now, now)).lastrowid


def update(project_id: int, **fields) -> None:
    allowed = {"name", "description", "goal", "status", "location_name", "lat", "lon"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "status" in sets and sets["status"] not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if not sets:
        return
    sets["updated_at"] = time.time()
    db.x(f"UPDATE projects SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), project_id))


def add_items(project_id: int, item_ids: list[int], role: str = "footage", note: str = "") -> int:
    if role not in ROLES:
        role = "reference"
    now = time.time()
    n = 0
    with db.tx():
        for iid in item_ids:
            if db.q1("SELECT 1 FROM items WHERE id=?", (iid,)):
                db.x("INSERT INTO project_items(project_id, item_id, role, note, added_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(project_id, item_id) DO UPDATE SET role=excluded.role, "
                     "note=CASE WHEN excluded.note!='' THEN excluded.note ELSE project_items.note END",
                     (project_id, iid, role, note, now))
                n += 1
        db.x("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
    return n


def remove_items(project_id: int, item_ids: list[int]) -> None:
    marks = ",".join("?" * len(item_ids))
    db.x(f"DELETE FROM project_items WHERE project_id=? AND item_id IN ({marks})", (project_id, *item_ids))


def add_note(project_id: int, body: str, author: str) -> int:
    nid = db.x("INSERT INTO project_notes(project_id, body, author, created_at) VALUES(?,?,?,?)",
               (project_id, body.strip(), author, time.time())).lastrowid
    db.x("UPDATE projects SET updated_at=? WHERE id=?", (time.time(), project_id))
    return nid


def save_plan(project_id: int, plan_md: str) -> None:
    db.x("UPDATE projects SET plan=?, plan_updated_at=?, updated_at=? WHERE id=?",
         (plan_md, time.time(), time.time(), project_id))


def listing() -> list[dict]:
    rows = db.q("SELECT p.*, (SELECT COUNT(*) FROM project_items pi WHERE pi.project_id=p.id) AS item_count, "
                "(SELECT pi.item_id FROM project_items pi JOIN items i ON i.id=pi.item_id "
                " WHERE pi.project_id=p.id AND i.thumb_state='ok' ORDER BY pi.added_at LIMIT 1) AS cover_id "
                "FROM projects p ORDER BY p.updated_at DESC")
    for r in rows:
        r.pop("plan", None)
    return rows


def detail(project_id: int) -> dict | None:
    p = db.q1("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        return None
    rows = db.q(f"SELECT {catalog.ITEM_COLS}, pi.role, pi.note FROM project_items pi JOIN items i ON i.id=pi.item_id "
                f"JOIN libraries l ON l.id=i.library_id WHERE pi.project_id=? AND i.trashed_at IS NULL "
                f"ORDER BY i.taken_at", (project_id,))
    items = catalog.cards(rows)
    for c, r in zip(items, rows):
        c["role"], c["note"] = r["role"], r["note"]
    p["items"] = items
    p["notes"] = db.q("SELECT * FROM project_notes WHERE project_id=? ORDER BY created_at DESC", (project_id,))
    p["summary"] = summarize(items)
    return p


def summarize(items: list[dict]) -> dict:
    """Facts the planner (human or AI) needs: what we have, when, where, and how much."""
    roles = Counter(i.get("role") for i in items)
    kinds = Counter(i["kind"] for i in items)
    footage_s = sum(i["duration"] or 0 for i in items if i["kind"] in ("video", "red_clip", "braw_clip"))
    days: dict[str, list[dict]] = defaultdict(list)
    for i in items:
        if i["taken_at"]:
            days[datetime.fromtimestamp(i["taken_at"]).strftime("%Y-%m-%d")].append(i)
    places = [(i["lat"], i["lon"]) for i in items if i.get("lat") is not None]
    return {
        "counts_by_kind": dict(kinds),
        "counts_by_role": dict(roles),
        "footage_minutes": round(footage_s / 60, 1),
        "days": [{"date": d, "items": len(v), "kinds": dict(Counter(x["kind"] for x in v))} for d, v in sorted(days.items())],
        "geotagged": len(places),
        "centroid": [sum(p[0] for p in places) / len(places), sum(p[1] for p in places) / len(places)] if places else None,
    }


def outline_for_ai(project_id: int, max_items: int = 300) -> str:
    """A compact, model-friendly catalog of the project (used as context for planning)."""
    p = detail(project_id)
    if not p:
        return ""
    lines = [f"PROJECT: {p['name']} (status: {p['status']})",
             f"Description: {p['description'] or '-'}", f"Goal: {p['goal'] or '-'}",
             f"Location: {p['location_name'] or '-'}", f"Summary: {json.dumps(p['summary'])}", "", "ITEMS:"]
    for i in p["items"][:max_items]:
        bits = [f"#{i['id']}", i["kind"], f"role={i.get('role')}", f'"{i["title"]}"']
        if i["date"]:
            bits.append(i["date"])
        if i["duration"]:
            bits.append(f"{i['duration']:.0f}s")
        if i.get("width"):
            bits.append(f"{i['width']}x{i['height']}")
        for k in ("camera", "reel", "clip", "camera_model", "format", "print_time", "filament_type", "artist"):
            if i.get(k):
                bits.append(f"{k}={i[k]}")
        if i.get("lat") is not None:
            bits.append(f"gps={i['lat']:.4f},{i['lon']:.4f}")
        if i.get("tags"):
            bits.append("tags=" + "|".join(i["tags"][:8]))
        if i.get("note"):
            bits.append(f"note={i['note'][:120]}")
        lines.append(" ".join(str(b) for b in bits))
    if len(p["items"]) > max_items:
        lines.append(f"... and {len(p['items']) - max_items} more items")
    if p["notes"]:
        lines += ["", "NOTES:"] + [f"- ({datetime.fromtimestamp(n['created_at']):%Y-%m-%d}) {n['body'][:500]}"
                                   for n in p["notes"][:30]]
    return "\n".join(lines)


def template_plan(project_id: int) -> str:
    """Offline plan when no AI model is configured: chronological structure + checklists."""
    p = detail(project_id)
    if not p:
        return ""
    s = p["summary"]
    out = [f"# Video plan — {p['name']}", "", f"**Goal:** {p['goal'] or 'Describe what the viewer should take away.'}", "",
           f"Material on hand: {s['footage_minutes']} min of footage, "
           + ", ".join(f"{v} {k}" for k, v in s["counts_by_kind"].items()), "", "## Story outline", "",
           "1. **Hook (0:00–0:15)** — the most striking shot or the finished result first.",
           "2. **The problem / why** — what this project is and why it matters.",
           "3. **The build / journey** — chronological, one section per day below.",
           "4. **The payoff** — result, test, ride, reveal.",
           "5. **Wrap-up** — what you learned, what's next.", "", "## Timeline by day", ""]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for i in p["items"]:
        d = i["date"][:10] if i["date"] else "Undated"
        by_day[d].append(i)
    for day, items in sorted(by_day.items()):
        out.append(f"### {day}")
        for i in items:
            dur = f" ({i['duration']:.0f}s)" if i["duration"] else ""
            out.append(f"- [ ] #{i['id']} {i['kind']}: {i['title']}{dur}")
        out.append("")
    out += ["## Shots to capture", "", "- [ ] Establishing wide of the space / location",
            "- [ ] Close-up detail shots of key parts", "- [ ] Before / after comparison", "- [ ] Piece to camera intro & outro",
            "- [ ] Clean ambient audio (30 s room tone)", ""]
    return "\n".join(out)
