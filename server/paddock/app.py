"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db, federation, jobs, mcp, portmap, tls, transcode, wireguard
from .api import admin, assistant, auth, federation as federation_api, libraries, media, party as party_api, projects, share, system as system_api, uploads, voice as voice_api
from .api.deps import client_ip, network_zone
from .config import settings

log = logging.getLogger("paddock")
WEB_DIR = Path(__file__).parent / "web"

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; script-src 'self'; "
       "style-src 'self' 'unsafe-inline'; worker-src 'self' blob:; connect-src 'self'; font-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; frame-src 'self'; form-action 'self'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    wireguard.init_subnet()
    tls.ensure_certificates()  # also this server's identity for linked servers
    jobs.mark_interrupted()
    federation.refresh_peer_ips()
    if settings.wg_enabled:
        wireguard.server_keys()
        wireguard.sync()
    heartbeat = federation.Heartbeat()
    renewer = portmap.Renewer()
    transcode.segments.start()
    threading.Thread(target=transcode.detect_hardware, daemon=True, name="hw-detect").start()
    scheduler = jobs.Scheduler()
    if not settings.dev_mode or db.get_setting("scheduler_in_dev", False):
        scheduler.start()
        heartbeat.start()
        renewer.start()
    from .party import parties

    party_watchdog = asyncio.create_task(parties.watchdog())
    log.info("Paddock %s ready — data in %s", __version__, settings.data_dir)
    yield
    scheduler.stop.set()
    heartbeat.stop.set()
    renewer.stop.set()
    transcode.segments.stop_all()
    party_watchdog.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="Paddock", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs" if settings.dev_mode else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if settings.dev_mode else None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # 1) Network policy: only the home LAN, the WireGuard tunnel and loopback may talk to Paddock.
        #    Linked servers' tunnel addresses may only reach the server-to-server API.
        zone = network_zone(client_ip(request))
        if zone is None:
            return PlainTextResponse("Forbidden", status_code=403)
        if zone == "peer" and not (request.url.path.startswith("/api/peer/") or request.url.path in ("/api/ca.crt", "/api/health")):
            return PlainTextResponse("Forbidden", status_code=403)
        response = await call_next(request)
        # 2) Hardening headers on every response.
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(self), geolocation=(), payment=()")  # mic: voice chat
        if settings.tls:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith("/api/") and "cache-control" not in h:
            h["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/") and "/vendor/" not in request.url.path:
            h["Cache-Control"] = "no-cache"  # revalidate (cheap 304 via ETag) so upgrades never mix old and new code
        return response

    @app.exception_handler(PermissionError)
    async def perm(request: Request, exc: PermissionError):
        return JSONResponse({"detail": str(exc) or "Not allowed"}, status_code=403)

    for r in (auth.router, libraries.router, media.router, projects.router, uploads.router, admin.router,
              assistant.router, share.router, federation_api.router, system_api.router, party_api.router,
              voice_api.router, mcp.router):
        app.include_router(r)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/download/android")
    def android_apk():
        """The Android app, served from data/downloads/paddock.apk (home network / tunnel only, like everything else)."""
        apk = settings.data_dir / "downloads" / "paddock.apk"
        if not apk.exists():
            return JSONResponse({"detail": "No Android app has been uploaded to this server yet"}, status_code=404)
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="paddock.apk")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

        @app.get("/manifest.webmanifest")
        def manifest():
            return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

        @app.get("/sw.js")
        def service_worker():
            return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript",
                                headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
