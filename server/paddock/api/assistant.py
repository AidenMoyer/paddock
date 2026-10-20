"""Assistant chat API (any signed-in user)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import ai, db, jobs
from ..actor import Actor
from .deps import current_actor, require_write

router = APIRouter(prefix="/api/ai")


@router.get("/info")
def info(actor: Actor = Depends(current_actor)):
    cfg = ai.config()
    user = db.q1("SELECT allow_ai_changes FROM users WHERE id=?", (actor.user_id,))
    return {"provider": cfg["provider"], "model": ai.CLAUDE_MODEL if cfg["provider"] == "claude" else cfg["model"],
            "offsite": cfg["provider"] == "claude", "can_write": bool(user and user["allow_ai_changes"]),
            "available": cfg["provider"] != "none"}


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    project_id: int | None = None


@router.post("/chat")
async def chat(body: ChatBody, actor: Actor = Depends(current_actor)):
    if actor.kind == "token":
        raise HTTPException(403, "Agents should use MCP directly")
    res = await run_in_threadpool(ai.chat, actor, body.message, body.conversation_id, body.project_id)
    if "error" in res:
        raise HTTPException(409, res["error"])
    return res


class TagBody(BaseModel):
    item_ids: list[int] = Field(max_length=2000)


@router.post("/autotag")
def autotag(body: TagBody, actor: Actor = Depends(require_write)):
    if ai.config()["provider"] == "none":
        raise HTTPException(409, "No AI model configured")
    ids = body.item_ids
    return {"job_id": jobs.submit("autotag", f"items:{ids[0] if ids else 0}:{len(ids)}", lambda ctx: ai.autotag(ids, ctx))}
