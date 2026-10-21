"""Projects REST API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import access, ai, db, jobs, projects
from ..actor import Actor
from .deps import current_actor, require_write

router = APIRouter(prefix="/api/projects")


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    goal: str = ""
    status: str = "active"
    kind: str = "video"
    location_name: str | None = None
    lat: float | None = None
    lon: float | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    goal: str | None = None
    status: str | None = None
    kind: str | None = None
    location_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    upload_library_id: int | None = None


def _project(pid: int, actor: Actor, write: bool = False) -> dict:
    p = db.q1("SELECT * FROM projects WHERE id=?", (pid,))
    level = access.project_access(actor, p) if p else None
    if not level:
        raise HTTPException(404, "No such project")
    if write and level != "write":
        raise HTTPException(403, "You can look at this project, but not change it")
    return p


def _not_media_only(actor: Actor) -> None:
    if not actor.is_admin and access.media_only(actor.user_id):
        raise HTTPException(403, "Projects aren't available in media-only mode")


@router.get("")
def list_projects(actor: Actor = Depends(current_actor)):
    return projects.listing(actor)


@router.get("/kinds")
def kinds(actor: Actor = Depends(current_actor)):
    return [{"id": k, "label": v} for k, v in projects.KINDS.items()]


@router.post("")
def create(body: ProjectBody, actor: Actor = Depends(require_write)):
    _not_media_only(actor)
    pid = projects.create(body.name, body.description, body.goal, body.location_name, body.lat, body.lon, body.status,
                          body.kind, created_by=actor.user_id)
    db.audit(actor.audit_name, "project.created", project=pid, name=body.name)
    return {"id": pid}


@router.get("/{pid}")
def detail(pid: int, actor: Actor = Depends(current_actor)):
    p = projects.detail(pid, actor)
    if not p:
        raise HTTPException(404, "No such project")
    return p


@router.patch("/{pid}")
def update(pid: int, body: ProjectPatch, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    if body.upload_library_id is not None and not access.can_write_library(actor, body.upload_library_id):
        raise HTTPException(403, "You can't add files to that library")
    try:
        projects.update(pid, **body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/{pid}")
def delete(pid: int, actor: Actor = Depends(require_write)):
    """Deletes the project only — its media stays in your libraries."""
    _project(pid, actor, write=True)
    db.x("DELETE FROM projects WHERE id=?", (pid,))
    db.audit(actor.audit_name, "project.deleted", project=pid)
    return {"ok": True}


class ShareBody(BaseModel):
    communities: list[dict] = Field(default_factory=list, max_length=100)   # [{id, access: read|write}]


@router.put("/{pid}/access")
def set_access(pid: int, body: ShareBody, actor: Actor = Depends(require_write)):
    """Share a project with communities (none = open to everyone, as before communities existed)."""
    _project(pid, actor, write=True)
    rows = []
    for c in body.communities:
        cid, level = int(c.get("id", 0)), c.get("access", "write")
        if level not in ("read", "write") or not db.q1("SELECT 1 FROM communities WHERE id=?", (cid,)):
            raise HTTPException(400, "Unknown community or access level")
        if not actor.is_admin and not access.is_member(actor.user_id, cid):
            raise HTTPException(403, "You can only share with communities you're in")
        rows.append((pid, cid, level))
    with db.tx():
        db.x("DELETE FROM project_access WHERE project_id=?", (pid,))
        for r in rows:
            db.x("INSERT INTO project_access(project_id, community_id, access) VALUES(?,?,?)", r)
    db.audit(actor.audit_name, "project.access", project=pid, communities=[r[1] for r in rows])
    return projects.detail(pid, actor)


class ItemsBody(BaseModel):
    item_ids: list[int] = Field(max_length=5000)
    role: str = "footage"
    note: str = ""


@router.post("/{pid}/items")
def add_items(pid: int, body: ItemsBody, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    allowed = access.library_ids(actor)
    ids = body.item_ids
    if allowed is not None and ids:
        ids = [r["id"] for r in db.q(f"SELECT id, library_id FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)
               if r["library_id"] in allowed]
    return {"added": projects.add_items(pid, ids, body.role, body.note)}


@router.post("/{pid}/items/remove")
def remove_items(pid: int, body: ItemsBody, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    projects.remove_items(pid, body.item_ids)
    return {"ok": True}


class NoteBody(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


@router.post("/{pid}/notes")
def add_note(pid: int, body: NoteBody, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    return {"id": projects.add_note(pid, body.body, actor.username)}


@router.delete("/{pid}/notes/{nid}")
def delete_note(pid: int, nid: int, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    db.x("DELETE FROM project_notes WHERE id=? AND project_id=?", (nid, pid))
    return {"ok": True}


class PlanBody(BaseModel):
    plan: str = Field(max_length=200_000)


@router.put("/{pid}/plan")
def save_plan(pid: int, body: PlanBody, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    projects.save_plan(pid, body.plan)
    return {"ok": True}


class GenerateBody(BaseModel):
    direction: str = Field(default="", max_length=4000)


@router.post("/{pid}/plan/generate")
def generate_plan(pid: int, body: GenerateBody, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    _not_media_only(actor)
    try:
        return ai.generate_plan(actor, pid, body.direction)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{pid}/autotag")
def autotag(pid: int, actor: Actor = Depends(require_write)):
    _project(pid, actor, write=True)
    ids = [r["item_id"] for r in db.q("SELECT item_id FROM project_items WHERE project_id=?", (pid,))]
    return {"job_id": jobs.submit("autotag", f"project:{pid}", lambda ctx: ai.autotag(ids, ctx))}


@router.post("/{pid}/transcribe")
def transcribe(pid: int, actor: Actor = Depends(require_write)):
    from .. import transcribe as tr

    _project(pid, actor, write=True)
    if not tr.enabled():
        raise HTTPException(409, "Transcription isn't set up — see Settings → Transcription")
    n = 0
    for r in db.q("SELECT i.* FROM project_items pi JOIN items i ON i.id=pi.item_id WHERE pi.project_id=? "
                  "AND i.kind IN ('video','red_clip','audio')", (pid,)):
        n += tr.transcribe_all(r)
    return {"queued_chunks": n}
