"""Public links: one video, its own password, watchable from anywhere on the admin's own domain.

Everything else about Paddock stays private. Public viewers reach a *separate* listener that only knows
the routes in this file — the password page, the player, and that one video's stream — so nothing from
the main app (API, library, accounts) is reachable from the internet.

  https://<domain>/v/<token>   HTTPS on settings.public_port (router: TCP 443 → here), Let's Encrypt certificate
  http://<domain>/...          HTTP on settings.acme_port (router: TCP 80 → here): certificate checks + redirect

Protection:
- every link has a password (scrypt-hashed like account passwords) and an expiry, and can be turned off;
- wrong passwords: 8 per address per 15 minutes, then that address waits 15 minutes; 25 per link per hour
  (from anywhere) and the link locks for an hour — both are written to the audit log;
- unknown, expired and revoked links look exactly like a wrong password until the right one is given;
- a viewer's unlock is a signed cookie for that link only, void as soon as the password changes;
- at most `max_viewers` people watch at once, and only up to 1080p (the home upload is the limit).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import acme, db, portmap, security, thumbs, transcode
from .config import settings
from .scanner import safe_item_path

log = logging.getLogger("paddock.public")

SETTING = "public_links"
DEFAULTS = {"enabled": False, "domain": "", "email": "", "agreed": False, "auto_portmap": True, "max_viewers": 4}
PUBLIC_PROFILES = ("1080p", "720p", "480p", "360p")
PLAYABLE = ("video", "red_clip")
SESSION_S = 12 * 3600
COOKIE = "paddock_view"
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
STATIC = Path(__file__).resolve().parent / "web" / "static"
PUBLIC_FILES = {"tokens.css": ("tokens.css", "text/css"), "public.css": ("public/public.css", "text/css"),
                "public.js": ("public/public.js", "text/javascript"),
                "hls.min.js": ("vendor/hls.min.js", "text/javascript"), "icon.svg": ("icon.svg", "image/svg+xml")}



def config() -> dict:
    return DEFAULTS | (db.get_setting(SETTING) or {})


def tls_dir() -> Path:
    return settings.data_dir / "public-tls"


def valid_domain(domain: str) -> bool:
    try:
        ipaddress.ip_address(domain)
        return False
    except ValueError:
        return bool(DOMAIN_RE.match(domain)) and not domain.endswith((".local", ".lan", ".home.arpa", ".internal"))


# ---- links ------------------------------------------------------------------------------------

def link_url(token: str) -> str:
    return f"https://{config()['domain']}/v/{token}"


def create_link(item_id: int, password: str, user_id: int, days: float | None, allow_download: bool, label: str) -> dict:
    item = db.q1("SELECT id, kind, title, trashed_at FROM items WHERE id=?", (item_id,))
    if not item or item["trashed_at"] or item["kind"] not in PLAYABLE:
        raise ValueError("Only videos can be shared online")
    if len(password) < 8:
        raise ValueError("The password needs at least 8 characters")
    token = secrets.token_urlsafe(16)
    now = time.time()
    cur = db.x("INSERT INTO public_links(token, item_id, created_by, created_at, expires_at, password_hash, allow_download, label)"
               " VALUES(?,?,?,?,?,?,?,?)", (token, item_id, user_id, now, now + days * 86400 if days else None,
                                           security.hash_password(password), int(allow_download and item["kind"] == "video"),
                                           label.strip()[:120] or None))
    return link(cur.lastrowid)


def link(link_id: int) -> dict | None:
    row = db.q1("SELECT l.*, i.title AS item_title FROM public_links l LEFT JOIN items i ON i.id=l.item_id WHERE l.id=?", (link_id,))
    return _public_view(row) if row else None


def links(item_id: int | None = None) -> list[dict]:
    sql = "SELECT l.*, i.title AS item_title FROM public_links l LEFT JOIN items i ON i.id=l.item_id"
    rows = db.q(sql + (" WHERE l.item_id=?" if item_id else "") + " ORDER BY l.created_at DESC", (item_id,) if item_id else ())
    return [_public_view(r) for r in rows]


def _public_view(r: dict) -> dict:
    now = time.time()
    state = ("off" if r["revoked_at"] else "expired" if r["expires_at"] and r["expires_at"] < now
             else "locked" if r["locked_until"] and r["locked_until"] > now else "active")
    return {"id": r["id"], "item_id": r["item_id"], "item_title": r.get("item_title"), "url": link_url(r["token"]),
            "created_at": r["created_at"], "expires_at": r["expires_at"], "allow_download": bool(r["allow_download"]),
            "label": r["label"], "views": r["views"], "last_view_at": r["last_view_at"], "state": state,
            "locked_until": r["locked_until"] if state == "locked" else None, "viewers": viewers.count(r["id"])}


def revoke(link_id: int) -> None:
    db.x("UPDATE public_links SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (time.time(), link_id))
    viewers.drop_link(link_id)


def set_password(link_id: int, password: str) -> None:
    if len(password) < 8:
        raise ValueError("The password needs at least 8 characters")
    db.x("UPDATE public_links SET password_hash=?, locked_until=NULL WHERE id=?", (security.hash_password(password), link_id))
    viewers.drop_link(link_id)        # old unlocks are void (their signature covers the old hash)


def _row(token: str) -> dict | None:
    if not token or len(token) > 64:
        return None
    return db.q1("SELECT * FROM public_links WHERE token=?", (token,))


def _usable(row: dict | None) -> bool:
    now = time.time()
    return bool(row and not row["revoked_at"] and not (row["expires_at"] and row["expires_at"] < now))


# ---- wrong-password limits -------------------------------------------------------------------

class Limiter:
    IP_MAX, IP_WINDOW, IP_BLOCK = 8, 900, 900
    LINK_MAX, LINK_WINDOW, LINK_LOCK = 25, 3600, 3600

    def __init__(self):
        self.lock = threading.Lock()
        self.by_ip: dict[str, deque] = defaultdict(deque)
        self.by_link: dict[int, deque] = defaultdict(deque)
        self.blocked: dict[str, float] = {}
        self.checking = threading.BoundedSemaphore(4)       # scrypt is deliberately slow; don't let a flood queue up

    def wait(self, ip: str, row: dict | None) -> float:
        now = time.time()
        with self.lock:
            until = self.blocked.get(ip, 0)
        if row and row["locked_until"] and row["locked_until"] > now:
            until = max(until, row["locked_until"])
        return max(0.0, until - now)

    def failed(self, ip: str, row: dict | None) -> None:
        now = time.time()
        with self.lock:
            q = self.by_ip[ip]
            q.append(now)
            while q and q[0] < now - self.IP_WINDOW:
                q.popleft()
            if len(q) >= self.IP_MAX:
                self.blocked[ip] = now + self.IP_BLOCK
                q.clear()
                db.audit("public", "public.address_blocked", ip=ip, minutes=self.IP_BLOCK // 60)
            if row:
                lq = self.by_link[row["id"]]
                lq.append(now)
                while lq and lq[0] < now - self.LINK_WINDOW:
                    lq.popleft()
                if len(lq) >= self.LINK_MAX:
                    lq.clear()
                    db.x("UPDATE public_links SET locked_until=? WHERE id=?", (now + self.LINK_LOCK, row["id"]))
                    db.audit("public", "public.link_locked", link=row["id"], minutes=self.LINK_LOCK // 60)
            if len(self.by_ip) > 20000:                       # forget the oldest addresses
                for k in list(self.by_ip)[:10000]:
                    self.by_ip.pop(k, None)
            for k in [k for k, t in self.blocked.items() if t < now]:
                self.blocked.pop(k, None)

    def succeeded(self, ip: str) -> None:
        with self.lock:
            self.by_ip.pop(ip, None)


limiter = Limiter()


# ---- viewers ----------------------------------------------------------------------------------

def _secret() -> bytes:
    s = db.get_setting("public_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("public_secret", s)
    return bytes.fromhex(s)


def _signature(row: dict, exp: int, nonce: str) -> str:
    msg = f"{row['id']}.{exp}.{nonce}.{row['password_hash'][-24:]}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_cookie(row: dict) -> tuple[str, int]:
    exp = int(time.time() + SESSION_S)
    if row["expires_at"]:
        exp = min(exp, int(row["expires_at"]))
    nonce = secrets.token_urlsafe(12)
    return f"{exp}.{nonce}.{_signature(row, exp, nonce)}", max(1, exp - int(time.time()))


def check_cookie(row: dict, value: str | None) -> str | None:
    """The viewer's id if the cookie unlocks this link, else None."""
    try:
        exp_s, nonce, sig = (value or "").split(".")
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < time.time() or not hmac.compare_digest(sig, _signature(row, exp, nonce)):
        return None
    return nonce


class Viewers:
    """Who's watching right now (anyone fetching the stream in the last minute)."""
    ACTIVE_S = 60

    def __init__(self):
        self.lock = threading.Lock()
        self.seen: dict[str, tuple[int, float]] = {}

    def _prune(self, now: float) -> None:
        for k in [k for k, (_, t) in self.seen.items() if t < now - self.ACTIVE_S]:
            self.seen.pop(k, None)

    def admit(self, nonce: str, link_id: int, limit: int) -> bool:
        now = time.time()
        with self.lock:
            self._prune(now)
            if nonce not in self.seen and len(self.seen) >= limit:
                return False
            self.seen[nonce] = (link_id, now)
            return True

    def count(self, link_id: int | None = None) -> int:
        with self.lock:
            self._prune(time.time())
            return sum(1 for lid, _ in self.seen.values() if link_id is None or lid == link_id)

    def drop_link(self, link_id: int) -> None:
        with self.lock:
            for k in [k for k, (lid, _) in self.seen.items() if lid == link_id]:
                self.seen.pop(k, None)


viewers = Viewers()


# ---- the public site ------------------------------------------------------------------------------

def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><meta name=\"robots\" content=\"noindex, nofollow\">"
           "<meta name=\"referrer\" content=\"no-referrer\"><meta name=\"color-scheme\" content=\"dark light\">"
           f"<title>{html.escape(title)}</title><link rel=\"icon\" href=\"/_p/icon.svg\"><link rel=\"stylesheet\" href=\"/_p/tokens.css\">"
           "<link rel=\"stylesheet\" href=\"/_p/public.css\">"
           f"</head><body><main class=\"pv\">{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def _lock_page(error: str = "", status: int = 200) -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(error, quote=False)}</p>' if error else ""
    return _page("Shared video · Paddock", (
        '<div class="lock"><img src="/_p/icon.svg" alt="" width="56" height="56">'
        "<h1>Enter the password</h1><p>Someone shared a video with you. Type the password they gave you to watch it.</p>"
        '<form method="post"><label for="pw">Password</label>'
        '<input id="pw" type="password" name="password" autocomplete="current-password" required maxlength="256" autofocus>'
        f'<button type="submit">Watch</button></form>{err}</div>'), status)


def _player_page(row: dict, item: dict) -> HTMLResponse:
    t = row["token"]
    title = item["title"] or "Video"
    label = f'<p class="note">{html.escape(row["label"], quote=False)}</p>' if row["label"] else ""
    dl = f'<a class="dl" href="/v/{t}/download">Download</a>' if row["allow_download"] else ""
    exp = (f'<p class="muted">This link works until {time.strftime("%d %b %Y", time.localtime(row["expires_at"]))}.</p>'
           if row["expires_at"] else "")
    body = (f'<header><img src="/_p/icon.svg" alt="" width="26" height="26"><span>Shared with Paddock</span></header>'
            f'<video id="v" controls playsinline preload="metadata" poster="/v/{t}/poster" data-src="/v/{t}/hls/master.m3u8"></video>'
            f'<p id="msg" class="err" hidden></p><div class="meta"><h1>{html.escape(title, quote=False)}</h1>{dl}</div>{label}{exp}'
            '<script src="/_p/hls.min.js"></script><script src="/_p/public.js"></script>')
    return _page(f"{title} · Paddock", body)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _viewer(request: Request, token: str) -> tuple[dict, dict, str]:
    """(link, item, viewer id) for a stream request, or 404/503."""
    row = _row(token)
    if not _usable(row):
        raise HTTPException(404, "Not found")
    nonce = check_cookie(row, request.cookies.get(COOKIE))
    if not nonce:
        raise HTTPException(404, "Not found")
    item = db.q1("SELECT * FROM items WHERE id=?", (row["item_id"],))
    if not item or item["trashed_at"] or item["kind"] not in PLAYABLE:
        raise HTTPException(404, "Not found")
    if not viewers.admit(nonce, row["id"], int(config()["max_viewers"])):
        raise HTTPException(503, "Too many people are watching right now. Try again in a few minutes.")
    return row, item, nonce


def _source(item: dict, meta: dict) -> Path:
    from .api.media import _source_for
    return _source_for(item, meta)


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000",
}


def create_public_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        cfg = config()
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if not cfg["enabled"] or host != cfg["domain"]:
            resp: Response = PlainTextResponse("Not found", status_code=404)
        else:
            resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers={"Cache-Control": "no-store"})

    @app.get("/_p/{name}")
    def asset(name: str):
        if name not in PUBLIC_FILES:
            raise HTTPException(404, "Not found")
        rel, mime = PUBLIC_FILES[name]
        return FileResponse(STATIC / rel, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/v/{token}")
    def view(token: str, request: Request):
        row = _row(token)
        if _usable(row) and check_cookie(row, request.cookies.get(COOKIE)):
            item = db.q1("SELECT * FROM items WHERE id=?", (row["item_id"],))
            if item and not item["trashed_at"]:
                return _player_page(row, item)
        return _lock_page()

    @app.post("/v/{token}")
    async def unlock(token: str, request: Request):
        ip = _client_ip(request)
        if int(request.headers.get("content-length") or 0) > 4096:
            return PlainTextResponse("Too large", status_code=413)
        form = await request.form()
        password = str(form.get("password") or "")[:256]
        row = await run_in_threadpool(_row, token)
        wait = limiter.wait(ip, row)
        if wait:
            return _lock_page(f"Too many wrong passwords. Try again in {max(1, round(wait / 60))} minutes.", 429)
        if not limiter.checking.acquire(blocking=False):
            return _lock_page("The server is busy. Try again in a moment.", 503)
        try:
            if row:
                ok = await run_in_threadpool(security.verify_password, password, row["password_hash"])
            else:
                await run_in_threadpool(security.dummy_verify, password)
                ok = False
        finally:
            limiter.checking.release()
        if not ok:
            await run_in_threadpool(limiter.failed, ip, row)
            return _lock_page("That password isn't right.", 401)
        limiter.succeeded(ip)
        if not _usable(row):
            return _page("Link expired · Paddock", '<div class="lock"><img src="/_p/icon.svg" alt="" width="56" height="56">'
                         "<h1>This link has ended</h1><p>It expired or was turned off. Ask the person who shared it for a new one.</p></div>", 410)
        value, max_age = make_cookie(row)
        db.x("UPDATE public_links SET views=views+1, last_view_at=? WHERE id=?", (time.time(), row["id"]))
        db.audit("public", "public.watched", link=row["id"], item=row["item_id"], ip=ip)
        resp = RedirectResponse(f"/v/{token}", status_code=303)
        resp.set_cookie(COOKIE, value, max_age=max_age, path=f"/v/{token}", secure=True, httponly=True, samesite="lax")
        return resp

    @app.get("/v/{token}/poster")
    def poster(token: str, request: Request):
        row = _row(token)
        if not _usable(row) or not check_cookie(row, request.cookies.get(COOKIE)):
            raise HTTPException(404, "Not found")
        p = thumbs.thumb_path(row["item_id"])
        if not p.exists():
            raise HTTPException(404, "Not found")
        return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/v/{token}/hls/master.m3u8")
    def master(token: str, request: Request):
        _, item, _ = _viewer(request, token)
        lines = transcode.master_playlist(json.loads(item["meta"] or "{}"), False).splitlines()
        out = lines[:3]
        for inf, uri in zip(lines[3::2], lines[4::2]):
            if uri.split("/")[0] in PUBLIC_PROFILES:
                out += [inf, uri]
        if len(out) == 3:                                   # nothing at or under 1080p: offer the smallest rung
            out += lines[-2:]
        return PlainTextResponse("\n".join(out) + "\n", media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @app.get("/v/{token}/hls/{profile}/index.m3u8")
    def index(token: str, profile: str, request: Request):
        _, item, _ = _viewer(request, token)
        if profile not in PUBLIC_PROFILES:
            raise HTTPException(404, "Not found")
        meta = json.loads(item["meta"] or "{}")
        dur = item["duration"] or meta.get("duration")
        if not dur:
            raise HTTPException(409, "This video can't be streamed yet")
        return PlainTextResponse(transcode.media_playlist(transcode.fixed_segments(dur)),
                                 media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @app.get("/v/{token}/hls/{profile}/seg_{seg}.ts")
    async def segment(token: str, profile: str, seg: int, request: Request):
        _, item, _ = await run_in_threadpool(_viewer, request, token)
        if profile not in PUBLIC_PROFILES or seg < 0:
            raise HTTPException(404, "Not found")
        meta = json.loads(item["meta"] or "{}")
        src = await run_in_threadpool(_source, item, meta)
        path = await run_in_threadpool(transcode.segments.segment, item, meta, src, profile, seg)
        if not path:
            raise HTTPException(503, "The server couldn't prepare this part of the video")
        return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/v/{token}/download")
    def download(token: str, request: Request):
        row, item, _ = _viewer(request, token)
        if not row["allow_download"] or item["kind"] != "video":
            raise HTTPException(404, "Not found")
        p = safe_item_path(item)
        return FileResponse(p, filename=p.name, content_disposition_type="attachment", headers={"Cache-Control": "no-store"})

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def nothing(path: str):
        return PlainTextResponse("Not found", status_code=404)

    return app


# ---- port 80: certificate checks, and a redirect to HTTPS ---------------------------------------------

_challenges: dict[str, str] = {}


def create_challenge_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/.well-known/acme-challenge/{token}")
    def challenge(token: str):
        if token not in _challenges:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(_challenges[token])

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    def to_https(path: str, request: Request):
        domain = config()["domain"]
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if not domain or host != domain or not path.startswith("v/"):
            return PlainTextResponse("Not found", status_code=404)
        return RedirectResponse(f"https://{domain}/{path}", status_code=301)

    return app


# ---- listeners, certificate and router --------------------------------------------------------------

class _Listener:
    def __init__(self, app: FastAPI, port: int, cert: Path | None = None, key: Path | None = None):
        import uvicorn

        kw = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)} if cert else {}
        self.port = port
        self.stamp = cert.stat().st_mtime if cert else None
        self.server = uvicorn.Server(uvicorn.Config(
            app, host=settings.host, port=port, lifespan="off", proxy_headers=False, server_header=False, date_header=False,
            log_level="warning", timeout_keep_alive=15, limit_concurrency=200, **kw))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"public-{port}")
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


class Site:
    """Runs the public listeners while public links are on, keeps the certificate fresh and the router mapped."""
    RENEW_DAYS = 30

    def __init__(self):
        self.lock = threading.RLock()
        self.https: _Listener | None = None
        self.http: _Listener | None = None
        self.cert_error: str | None = None
        self.cert_busy = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    # files
    @property
    def cert_path(self) -> Path:
        return tls_dir() / "fullchain.pem"

    @property
    def key_path(self) -> Path:
        return tls_dir() / "privkey.pem"

    def certificate(self) -> dict | None:
        try:
            pem = self.cert_path.read_bytes()
            return {"names": acme.certificate_names(pem), "expires_at": acme.certificate_expiry(pem),
                    "days_left": acme.expires_in_days(pem)}
        except (OSError, ValueError):
            return None

    def cert_ok(self, domain: str) -> bool:
        c = self.certificate()
        return bool(c and domain in c["names"] and c["days_left"] > 0)

    # lifecycle
    def start(self) -> None:
        self.apply()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="public-site")
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.lock:
            for listener in (self.https, self.http):
                if listener:
                    listener.stop()
            self.https = self.http = None

    def apply(self) -> None:
        """Start, restart or stop the listeners to match the settings and the certificate on disk."""
        cfg = config()
        with self.lock:
            if not cfg["enabled"] or not cfg["domain"]:
                for listener in (self.https, self.http):
                    if listener:
                        listener.stop()
                self.https = self.http = None
                if portmap.public_state().get("ports"):
                    portmap.unmap_public()
                return
            try:
                if not self.http:
                    self.http = _Listener(create_challenge_app(), settings.acme_port)
                if self.cert_ok(cfg["domain"]):
                    stamp = self.cert_path.stat().st_mtime
                    if self.https and self.https.stamp != stamp:
                        self.https.stop()
                        self.https = None
                    if not self.https:
                        self.https = _Listener(create_public_app(), settings.public_port, self.cert_path, self.key_path)
            except OSError as e:
                log.error("public links: can't open the listening ports: %s", e)
                self.cert_error = f"Couldn't open port {settings.public_port} or {settings.acme_port}: {e}"
        if cfg["auto_portmap"]:
            threading.Thread(target=self.map_ports, daemon=True, name="public-portmap").start()

    def map_ports(self) -> dict:
        return portmap.map_public([(443, settings.public_port), (80, settings.acme_port)])

    def obtain(self, ctx=None) -> str:
        """Get (or renew) the certificate from Let's Encrypt. Blocking — run it as a job."""
        cfg = config()
        if not cfg["enabled"] or not cfg["agreed"] or not valid_domain(cfg["domain"]):
            raise RuntimeError("Public links are off, or the domain or the Let's Encrypt agreement is missing")
        with self.lock:
            if self.cert_busy:
                return "already running"
            self.cert_busy = True
        try:
            self.apply()                                     # the port-80 listener must be up for the check
            if ctx:
                ctx.progress(0.1, "Asking Let's Encrypt for a certificate")
            client = acme.Client(settings.acme_directory, acme.load_or_create_key(tls_dir() / "account.key"))
            client.account(cfg["email"] or None)
            if ctx:
                ctx.progress(0.3, f"Let's Encrypt is checking http://{cfg['domain']}")
            cert_key = acme.new_key()
            pem = client.issue(cfg["domain"], cert_key, lambda t, v: _challenges.__setitem__(t, v),
                               lambda t: _challenges.pop(t, None))
            acme.write_private(self.key_path, acme.key_pem(cert_key))
            acme.write_private(self.cert_path, pem)
            self.cert_error = None
            db.audit("system", "public.certificate", domain=cfg["domain"])
            self.apply()
            return f"certificate for {cfg['domain']} until {time.strftime('%Y-%m-%d', time.localtime(acme.certificate_expiry(pem)))}"
        except Exception as e:
            self.cert_error = str(e)[:400]
            log.warning("public links: certificate failed: %s", e)
            raise
        finally:
            self.cert_busy = False

    def _loop(self) -> None:
        last_map = 0.0
        while not self.stop_event.wait(300):
            cfg = config()
            if not cfg["enabled"]:
                continue
            if cfg["auto_portmap"] and time.time() - last_map > portmap.LEASE // 3:
                last_map = time.time()
                try:
                    self.map_ports()
                except Exception:  # noqa: BLE001
                    log.exception("public links: port mapping failed")
            c = self.certificate()
            due = not c or cfg["domain"] not in c["names"] or c["days_left"] < self.RENEW_DAYS
            # after a failure, wait 6 hours before trying again (Let's Encrypt rate-limits failed checks)
            if due and cfg["agreed"] and not self.cert_busy and time.time() - getattr(self, "_last_try", 0) > 6 * 3600:
                self._last_try = time.time()
                try:
                    self.obtain()
                except Exception:  # noqa: BLE001
                    pass

    def status(self) -> dict:
        cfg = config()
        pub = portmap.public_ip()
        dns: list[str] = []
        if cfg["domain"] and valid_domain(cfg["domain"]):
            try:
                dns = sorted({a[4][0] for a in socket.getaddrinfo(cfg["domain"], None, socket.AF_INET)})
            except OSError:
                dns = []
        return {
            "settings": cfg, "public_ip": pub, "cgnat": portmap.is_cgnat(pub), "lan_ip": portmap.lan_ip(),
            "dns": dns, "dns_ok": bool(pub and pub in dns),
            "certificate": self.certificate(), "certificate_ok": self.cert_ok(cfg["domain"]) if cfg["domain"] else False,
            "certificate_error": self.cert_error, "certificate_busy": self.cert_busy,
            "listening": {"https": bool(self.https), "http": bool(self.http)},
            "ports": {"https": settings.public_port, "http": settings.acme_port}, "router": portmap.public_state(),
            "viewers": viewers.count(),
        }


site = Site()
