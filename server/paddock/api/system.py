"""Manage server → System: drives, RAID pools, network, name, time, power and logs (Paddock OS)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db, system
from ..actor import Actor
from .deps import require_admin

router = APIRouter(prefix="/api/admin/system")


def agent(actor: Actor, op: str, audit: bool = True, timeout: float = 60, **args) -> dict:
    try:
        result = system.call(op, timeout=timeout, **args)
    except system.AgentError as e:
        raise HTTPException(400, str(e))
    if audit:
        db.audit(actor.audit_name, f"system.{op}", **{k: v for k, v in args.items() if k != "confirm"})
    return result


class DeviceBody(BaseModel):
    device: str = Field(min_length=5, max_length=64)


class FormatBody(BaseModel):
    disk: str = Field(min_length=5, max_length=64)
    label: str = Field(min_length=1, max_length=16)
    confirm: str = Field(max_length=64)


class PoolBody(BaseModel):
    name: str = Field(min_length=1, max_length=31)
    level: str = Field(pattern=r"^raid(0|1|5|6|10)$")
    disks: list[str] = Field(min_length=2, max_length=24)
    confirm: str = Field(max_length=64)


class PoolAddBody(BaseModel):
    disk: str = Field(min_length=5, max_length=64)
    grow: bool = False
    confirm: str = Field(max_length=64)


class PoolRemoveBody(BaseModel):
    device: str = Field(min_length=5, max_length=64)
    confirm: str = Field(max_length=64)


class ConfirmBody(BaseModel):
    confirm: str = Field(max_length=64)


class NetworkBody(BaseModel):
    mode: str = Field(pattern=r"^(dhcp|static)$")
    address: str | None = Field(default=None, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    dns: list[str] = Field(default_factory=list, max_length=3)


class HostnameBody(BaseModel):
    hostname: str = Field(min_length=1, max_length=40)


class TimezoneBody(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


class PowerBody(BaseModel):
    action: str = Field(pattern=r"^(reboot|poweroff|restart-paddock)$")


@router.get("")
def overview(actor: Actor = Depends(require_admin)):
    out = system.overview()
    out["timezones"] = system.timezones() if out["managed"] else []
    return out


@router.get("/disks")
def disks(actor: Actor = Depends(require_admin)):
    return agent(actor, "disks", audit=False, timeout=120)


@router.post("/disks/mount")
def mount(body: DeviceBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "mount", device=body.device)


@router.post("/disks/unmount")
def unmount(body: DeviceBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "unmount", device=body.device)


@router.post("/disks/format")
def format_disk(body: FormatBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "format", timeout=900, disk=body.disk, label=body.label, confirm=body.confirm)


@router.post("/pools")
def pool_create(body: PoolBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "pool_create", timeout=900, name=body.name, level=body.level, disks=body.disks, confirm=body.confirm)


@router.post("/pools/{name}/add")
def pool_add(name: str, body: PoolAddBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "pool_add", timeout=300, name=name, disk=body.disk, grow=body.grow, confirm=body.confirm)


@router.post("/pools/{name}/expand")
def pool_expand(name: str, actor: Actor = Depends(require_admin)):
    return agent(actor, "pool_expand", timeout=3600, name=name)


@router.post("/pools/{name}/remove")
def pool_remove(name: str, body: PoolRemoveBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "pool_remove_disk", name=name, device=body.device, confirm=body.confirm)


@router.post("/pools/{name}/check")
def pool_check(name: str, actor: Actor = Depends(require_admin)):
    return agent(actor, "pool_check", name=name)


@router.post("/pools/{name}/delete")
def pool_delete(name: str, body: ConfirmBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "pool_destroy", timeout=300, name=name, confirm=body.confirm)


@router.get("/network")
def network(actor: Actor = Depends(require_admin)):
    return agent(actor, "network", audit=False)


@router.put("/network")
def network_set(body: NetworkBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "network_set", **body.model_dump())


@router.post("/network/confirm")
def network_confirm(actor: Actor = Depends(require_admin)):
    return agent(actor, "network_confirm")


@router.put("/hostname")
def hostname(body: HostnameBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "hostname", hostname=body.hostname)


@router.put("/timezone")
def timezone(body: TimezoneBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "timezone", timezone=body.timezone)


@router.post("/power")
def power(body: PowerBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "power", action=body.action)


class UpdateSettingsBody(BaseModel):
    auto: bool
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


@router.get("/updates")
def updates(actor: Actor = Depends(require_admin)):
    return agent(actor, "update_status", audit=False)


@router.post("/updates/check")
def updates_check(actor: Actor = Depends(require_admin)):
    return agent(actor, "update_check", audit=False, timeout=90)


@router.post("/updates/install")
def updates_install(actor: Actor = Depends(require_admin)):
    return agent(actor, "update_apply")


@router.put("/updates/settings")
def updates_settings(body: UpdateSettingsBody, actor: Actor = Depends(require_admin)):
    return agent(actor, "update_settings", auto=body.auto, time=body.time)


@router.get("/logs")
def logs(unit: str = "paddock", lines: int = 300, actor: Actor = Depends(require_admin)):
    return agent(actor, "logs", audit=False, unit=unit, lines=lines)
