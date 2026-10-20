"""Send G-code to printers on the home network (Klipper/Moonraker, OctoPrint).

Printers are configured by an admin in Settings → Printers. Only private-network
addresses are accepted, so this can never be turned into a way to push data to
the internet.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import db


def configured() -> list[dict]:
    return db.get_setting("printers", [])


def public_view() -> list[dict]:
    return [{"name": p["name"], "type": p["type"], "url": p["url"]} for p in configured()]


def _private_url(url: str) -> bool:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not (ip.is_private or ip.is_link_local) or ip.is_loopback:
            return False
    return True


def validate(p: dict) -> dict:
    name = str(p.get("name", "")).strip()[:60]
    typ = p.get("type")
    url = str(p.get("url", "")).strip().rstrip("/")
    if not name or typ not in ("moonraker", "octoprint"):
        raise ValueError("Printer needs a name and type 'moonraker' or 'octoprint'.")
    if not _private_url(url):
        raise ValueError("Printer URL must be an http(s) address on your local network.")
    return {"name": name, "type": typ, "url": url, "api_key": str(p.get("api_key", ""))[:200]}


def send(item_id: int, printer_name: str, start: bool = False) -> dict:
    from .scanner import safe_item_path

    printer = next((p for p in configured() if p["name"].lower() == printer_name.lower()), None)
    if not printer:
        raise ValueError(f"No printer named '{printer_name}'. Configured: {[p['name'] for p in configured()]}")
    item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if not item or item["kind"] != "gcode":
        raise ValueError("Only G-code items can be sent to a printer.")
    if not _private_url(printer["url"]):
        raise ValueError("Printer URL no longer resolves to a local address.")
    path: Path = safe_item_path(item)
    headers = {"X-Api-Key": printer["api_key"]} if printer.get("api_key") else {}
    with path.open("rb") as fh, httpx.Client(timeout=120, verify=False) as client:  # printers use self-signed certs
        if printer["type"] == "moonraker":
            r = client.post(f"{printer['url']}/server/files/upload", headers=headers,
                            files={"file": (path.name, fh, "application/octet-stream")},
                            data={"root": "gcodes", **({"print": "true"} if start else {})})
        else:
            r = client.post(f"{printer['url']}/api/files/local", headers=headers,
                            files={"file": (path.name, fh, "application/octet-stream")},
                            data={"select": "true", "print": "true" if start else "false"})
    if r.status_code >= 300:
        raise ValueError(f"Printer refused the upload ({r.status_code}): {r.text[:200]}")
    db.audit("system", "printer.upload", item=item_id, printer=printer["name"], start=start)
    return {"ok": True, "printer": printer["name"], "file": path.name, "started": start}
