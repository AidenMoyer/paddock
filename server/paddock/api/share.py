"""Expiring share links — hand a print file or a project's CAD bundle to someone on your network/VPN.

Links only work where Paddock is reachable (home network / tunnel); nothing is
published to the internet. Each link has an expiry and an optional download cap.
"""

from __future__ import annotations

import html
import tempfile
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .. import db
from ..actor import Actor
from ..scanner import safe_item_path
from ..security import new_token, token_hash
from .deps import current_actor, require_write

router = APIRouter()


class ShareBody(BaseModel):
    item_id: int | None = None
    project_id: int | None = None
    hours: float = Field(default=72, gt=0, le=24 * 90)
    max_downloads: int | None = Field(default=None, ge=1, le=10000)
    label: str = Field(default="", max_length=120)


@router.post("/api/share")
def create(body: ShareBody, request: Request, actor: Actor = Depends(require_write)):
    if bool(body.item_id) == bool(body.project_id):
        raise HTTPException(400, "Share either one item or one project")
    if body.item_id and not db.q1("SELECT 1 FROM items WHERE id=? AND trashed_at IS NULL", (body.item_id,)):
        raise HTTPException(404, "No such item")
    if body.project_id and not db.q1("SELECT 1 FROM projects WHERE id=?", (body.project_id,)):
        raise HTTPException(404, "No such project")
    tok = new_token("pdk_share")
    now = time.time()
    db.x("INSERT INTO share_links(token_hash, item_id, project_id, created_by, created_at, expires_at, max_downloads, label) "
         "VALUES(?,?,?,?,?,?,?,?)", (token_hash(tok), body.item_id, body.project_id, actor.user_id, now,
                                     now + body.hours * 3600, body.max_downloads, body.label))
    db.audit(actor.audit_name, "share.created", item=body.item_id, project=body.project_id, hours=body.hours)
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/s/{tok}", "expires_at": now + body.hours * 3600}


@router.get("/api/share")
def list_links(actor: Actor = Depends(current_actor)):
    return db.q("SELECT substr(s.token_hash,1,12) AS id, s.item_id, s.project_id, s.created_at, s.expires_at, "
                "s.max_downloads, s.downloads, s.label, i.title AS item_title, p.name AS project_name "
                "FROM share_links s LEFT JOIN items i ON i.id=s.item_id LEFT JOIN projects p ON p.id=s.project_id "
                "WHERE s.created_by=? OR ?=1 ORDER BY s.created_at DESC", (actor.user_id, 1 if actor.is_admin else 0))


@router.delete("/api/share/{prefix}")
def revoke(prefix: str, actor: Actor = Depends(require_write)):
    if len(prefix) < 12:
        raise HTTPException(400, "Bad id")
    db.x("DELETE FROM share_links WHERE token_hash LIKE ? AND (created_by=? OR ?=1)",
         (prefix + "%", actor.user_id, 1 if actor.is_admin else 0))
    return {"ok": True}


def _link(token: str) -> dict:
    row = db.q1("SELECT * FROM share_links WHERE token_hash=?", (token_hash(token),))
    if not row or row["expires_at"] < time.time() or (row["max_downloads"] and row["downloads"] >= row["max_downloads"]):
        raise HTTPException(404, "This link has expired")
    return row


def _files(link: dict) -> list[tuple[Path, str]]:
    if link["item_id"]:
        item = db.q1("SELECT * FROM items WHERE id=?", (link["item_id"],))
        if not item:
            raise HTTPException(404, "Gone")
        return [(safe_item_path(item), Path(item["path"]).name)]
    items = db.q("SELECT i.* FROM project_items pi JOIN items i ON i.id=pi.item_id WHERE pi.project_id=? "
                 "AND i.kind IN ('model3d','gcode','cad','pcb','document') AND i.trashed_at IS NULL", (link["project_id"],))
    return [(safe_item_path(i), i["rel_path"]) for i in items]


@router.get("/s/{token}", response_class=HTMLResponse)
def landing(token: str):
    link = _link(token)
    files = _files(link)
    title = link["label"] or (files[0][1] if len(files) == 1 else "Shared files")
    rows = "".join(f"<li>{html.escape(name)} <span>{p.stat().st_size / 1e6:.1f} MB</span></li>" for p, name in files[:200])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font:16px system-ui,sans-serif;background:#0f0f0f;color:#f1f1f1;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{background:#1c1c1c;border-radius:16px;padding:28px;max-width:520px;width:calc(100% - 32px)}}
h1{{font-size:20px;margin:0 0 12px}} ul{{padding:0;list-style:none;margin:0 0 20px;color:#aaa;font-size:14px}}
li{{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid #2a2a2a}}
a.btn{{display:inline-block;background:#ff4e45;color:#fff;text-decoration:none;padding:12px 22px;border-radius:999px;font-weight:600}}
small{{display:block;color:#777;margin-top:14px}}</style></head><body><div class="card">
<h1>{html.escape(title)}</h1><ul>{rows}</ul><a class="btn" href="/s/{html.escape(token)}/download">Download</a>
<small>Shared from Paddock · expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(link['expires_at']))}</small>
</div></body></html>"""


@router.get("/s/{token}/download")
def download(token: str):
    link = _link(token)
    files = _files(link)
    if not files:
        raise HTTPException(404, "Nothing to download")
    db.x("UPDATE share_links SET downloads=downloads+1 WHERE token_hash=?", (link["token_hash"],))
    if len(files) == 1:
        p, name = files[0]
        return FileResponse(p, filename=Path(name).name)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p, name in files:
            z.write(p, arcname=name)
    tmp.close()
    proj = db.q1("SELECT name FROM projects WHERE id=?", (link["project_id"],))
    fname = f"{(proj or {}).get('name', 'project')}.zip"
    return FileResponse(tmp.name, filename=fname, background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)))
