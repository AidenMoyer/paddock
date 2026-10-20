"""Projects REST API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import ai, db, jobs, projects
from ..actor import Actor
from .deps import current_actor, require_write

router = APIRouter(prefix="/api/projects")


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    goal: str = ""
    status: str = "active"
    location_name: str | None = None
    lat: float | None = None
    lon: float | None = None


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    goal: str | None = None
    status: str | None = None
    location_name: str | None = None
    lat: float | None = None
    lon: float | None = None


@router.get("")
def list_projects(actor: Actor = Depends(current_actor)):
    return projects.listing()


@router.post("")
def create(body: ProjectBody, actor: Actor = Depends(require_write)):
    pid = projects.create(body.name, body.description, body.goal, body.location_name, body.lat, body.lon, body.status)
    db.audit(actor.audit_name, "project.created", project=pid, name=body.name)
    return {"id": pid}


@router.get("/{pid}")
def detail(pid: int, actor: Actor = Depends(current_actor)):
    p = projects.detail(pid)
    if not p:
        raise HTTPException(404, "No such project")
    return p


@router.patch("/{pid}")
def update(pid: int, body: ProjectPatch, actor: Actor = Depends(require_write)):
    try:
        projects.update(pid, **body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.delete("/{pid}")
def delete(pid: int, actor: Actor = Depends(require_write)):
    """Deletes the project only — its media stays in your libraries."""
    db.x("DELETE FROM projects WHERE id=?", (pid,))
    db.audit(actor.audit_name, "project.deleted", project=pid)
    return {"ok": True}


class ItemsBody(BaseModel):
    item_ids: list[int] = Field(max_length=5000)
    role: str = "footage"
    note: str = ""


@router.post("/{pid}/items")
def add_items(pid: int, body: ItemsBody, actor: Actor = Depends(require_write)):
    if not db.q1("SELECT 1 FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "No such project")
    return {"added": projects.add_items(pid, body.item_ids, body.role, body.note)}


@router.post("/{pid}/items/remove")
def remove_items(pid: int, body: ItemsBody, actor: Actor = Depends(require_write)):
    projects.remove_items(pid, body.item_ids)
    return {"ok": True}


class NoteBody(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


@router.post("/{pid}/notes")
def add_note(pid: int, body: NoteBody, actor: Actor = Depends(require_write)):
    return {"id": projects.add_note(pid, body.body, actor.username)}


@router.delete("/{pid}/notes/{nid}")
def delete_note(pid: int, nid: int, actor: Actor = Depends(require_write)):
    db.x("DELETE FROM project_notes WHERE id=? AND project_id=?", (nid, pid))
    return {"ok": True}


class PlanBody(BaseModel):
    plan: str = Field(max_length=200_000)


@router.put("/{pid}/plan")
def save_plan(pid: int, body: PlanBody, actor: Actor = Depends(require_write)):
    projects.save_plan(pid, body.plan)
    return {"ok": True}


class GenerateBody(BaseModel):
    direction: str = Field(default="", max_length=4000)


@router.post("/{pid}/plan/generate")
def generate_plan(pid: int, body: GenerateBody, actor: Actor = Depends(require_write)):
    try:
        return ai.generate_plan(actor, pid, body.direction)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{pid}/autotag")
def autotag(pid: int, actor: Actor = Depends(require_write)):
    ids = [r["item_id"] for r in db.q("SELECT item_id FROM project_items WHERE project_id=?", (pid,))]
    return {"job_id": jobs.submit("autotag", f"project:{pid}", lambda ctx: ai.autotag(ids, ctx))}


@router.post("/{pid}/transcribe")
def transcribe(pid: int, actor: Actor = Depends(require_write)):
    from .. import transcribe as tr

    if not tr.enabled():
        raise HTTPException(409, "Transcription isn't set up — see Settings → Transcription")
    n = 0
    for r in db.q("SELECT i.* FROM project_items pi JOIN items i ON i.id=pi.item_id WHERE pi.project_id=? "
                  "AND i.kind IN ('video','red_clip','audio')", (pid,)):
        n += tr.transcribe_all(r)
    return {"queued_chunks": n}
