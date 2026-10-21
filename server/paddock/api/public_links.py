"""Manage server → Public links: the domain + certificate, and the password-protected video links themselves."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import db, jobs, public
from ..actor import Actor
from .deps import require_admin

router = APIRouter()


class PublicSettingsBody(BaseModel):
    enabled: bool
    domain: str = Field(default="", max_length=253)
    email: str = Field(default="", max_length=254)
    agreed: bool = False
    auto_portmap: bool = True
    max_viewers: int = Field(default=4, ge=1, le=50)


class LinkBody(BaseModel):
    item_id: int
    password: str = Field(min_length=8, max_length=256)
    days: float | None = Field(default=7, gt=0, le=3650)      # None = until turned off
    allow_download: bool = False
    label: str = Field(default="", max_length=120)


class PasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=256)


def _queue_certificate() -> int:
    return jobs.submit("certificate", public.config()["domain"], lambda ctx: public.site.obtain(ctx))


@router.get("/api/admin/public")
async def status(actor: Actor = Depends(require_admin)):
    return await run_in_threadpool(public.site.status)


@router.put("/api/admin/public")
async def save(body: PublicSettingsBody, actor: Actor = Depends(require_admin)):
    domain = body.domain.strip().lower().rstrip(".")
    if body.enabled:
        if not public.valid_domain(domain):
            raise HTTPException(400, "Enter a domain you own, like videos.example.com")
        if not body.agreed:
            raise HTTPException(400, "Let's Encrypt only issues certificates if you accept its Subscriber Agreement")
    email = body.email.strip()
    if email and ("@" not in email or any(c in email for c in " \r\n<>")):
        raise HTTPException(400, "That email address doesn't look right")
    before = public.config()
    db.set_setting(public.SETTING, {"enabled": body.enabled, "domain": domain, "email": email, "agreed": body.agreed,
                                     "auto_portmap": body.auto_portmap, "max_viewers": body.max_viewers})
    db.audit(actor.audit_name, "public.settings", enabled=body.enabled, domain=domain)
    await run_in_threadpool(public.site.apply)
    out = await run_in_threadpool(public.site.status)
    if body.enabled and (not out["certificate_ok"] or before.get("domain") != domain):
        out["job_id"] = _queue_certificate()
    return out


@router.post("/api/admin/public/certificate")
def certificate(actor: Actor = Depends(require_admin)):
    cfg = public.config()
    if not cfg["enabled"]:
        raise HTTPException(400, "Turn public links on first")
    return {"job_id": _queue_certificate()}


@router.get("/api/public-links")
def list_links(item_id: int | None = None, actor: Actor = Depends(require_admin)):
    return public.links(item_id)


@router.post("/api/public-links")
async def create(body: LinkBody, actor: Actor = Depends(require_admin)):
    cfg = public.config()
    if not cfg["enabled"] or not cfg["domain"]:
        raise HTTPException(400, "Turn on public links first (Manage server → Public links)")
    try:
        out = await run_in_threadpool(public.create_link, body.item_id, body.password, actor.user_id, body.days,
                                      body.allow_download, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(actor.audit_name, "public.link_created", link=out["id"], item=body.item_id, days=body.days)
    return out


@router.put("/api/public-links/{link_id}/password")
async def change_password(link_id: int, body: PasswordBody, actor: Actor = Depends(require_admin)):
    if not public.link(link_id):
        raise HTTPException(404, "Not found")
    try:
        await run_in_threadpool(public.set_password, link_id, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit(actor.audit_name, "public.password_changed", link=link_id)
    return public.link(link_id)


@router.delete("/api/public-links/{link_id}")
def turn_off(link_id: int, actor: Actor = Depends(require_admin)):
    if not public.link(link_id):
        raise HTTPException(404, "Not found")
    public.revoke(link_id)
    db.audit(actor.audit_name, "public.link_off", link=link_id)
    return {"ok": True}
