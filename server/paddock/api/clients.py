"""The Paddock apps this server hands out, so phones update from their own server (no app store, no internet).

Each server release bundles the matching signed Android app (CI puts paddock.apk and release.json in
android-app/ next to the paddock package). The app asks on launch and offers the update when it's newer.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..actor import Actor
from ..config import settings
from .deps import current_actor

router = APIRouter(prefix="/api/app")


def android_release() -> dict | None:
    folder = settings.android_dir
    try:
        info = json.loads((folder / "release.json").read_text())
        apk = folder / "paddock.apk"
        if not apk.is_file() or apk.stat().st_size != info["size"]:
            return None
        return {"version_name": str(info["version_name"]), "version_code": int(info["version_code"]),
                "sha256": str(info["sha256"]), "size": int(info["size"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


@router.get("/android")
def android(actor: Actor = Depends(current_actor)):
    rel = android_release()
    if not rel:
        return {"available": False}
    return {"available": True, **rel, "url": "/api/app/android/paddock.apk"}


@router.get("/android/paddock.apk")
def android_apk(actor: Actor = Depends(current_actor)):
    rel = android_release()
    if not rel:
        raise HTTPException(404, "This server doesn't include the Android app")
    return FileResponse(settings.android_dir / "paddock.apk", media_type="application/vnd.android.package-archive",
                        filename=f"Paddock-{rel['version_name']}.apk", headers={"Cache-Control": "no-store"})
