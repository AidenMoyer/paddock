"""Manage server → System.

On Paddock OS, Paddock runs unprivileged and asks paddock-agent (os/overlay/usr/local/sbin) to do the
few things that need root: drives, RAID pools, network, hostname, time zone, power and logs. The agent
listens on a root:paddock Unix socket and accepts a fixed list of operations.

Anywhere else (TrueNAS, Docker, a Windows test box) the host OS owns those things, so the panel shows
read-only facts about the machine Paddock is running on.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import sys
from pathlib import Path

from .config import settings


class AgentError(Exception):
    pass


def socket_path() -> str:
    return os.environ.get("PADDOCK_AGENT_SOCKET", "/run/paddock-agent.sock")


def available() -> bool:
    return hasattr(socket, "AF_UNIX") and os.path.exists(socket_path())


def call(op: str, timeout: float = 60, **args) -> dict:
    if not available():
        raise AgentError("System management is available on Paddock OS; here the host manages drives and network.")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    buf = b""
    try:
        s.connect(socket_path())
        s.sendall((json.dumps({"op": op, "args": args}) + "\n").encode())
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        raise AgentError(f"The system agent didn't answer: {e}") from e
    finally:
        s.close()
    try:
        resp = json.loads(buf or b"{}")
    except ValueError as e:
        raise AgentError("The system agent sent an unreadable answer") from e
    if not resp.get("ok"):
        raise AgentError(resp.get("error") or "The operation failed")
    return resp["result"]


def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        return {}
    return {"total": out.get("MemTotal"), "available": out.get("MemAvailable")}


def host_info() -> dict:
    """What Paddock can see about the machine without any privileges."""
    info: dict = {"hostname": platform.node(), "platform": platform.platform(terse=True), "python": sys.version.split()[0],
                  "cores": os.cpu_count(), "memory": _meminfo()}
    try:
        info["uptime"] = float(Path("/proc/uptime").read_text().split()[0])
    except OSError:
        pass
    try:
        info["load"] = os.getloadavg()
    except (OSError, AttributeError):
        pass
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        info["container"] = True
    storage = []
    for label, p in [("Paddock data", settings.data_dir)] + [(str(r), r) for r in settings.media_roots]:
        try:
            du = shutil.disk_usage(p)
            storage.append({"label": label, "path": str(p), "total": du.total, "free": du.free})
        except OSError:
            continue
    info["storage"] = storage
    return info


def overview() -> dict:
    if available():
        try:
            return {"managed": True, "info": call("info", timeout=20)}
        except AgentError as e:
            return {"managed": True, "error": str(e), "info": host_info()}
    return {"managed": False, "info": host_info()}


def timezones() -> list[str]:
    try:
        import zoneinfo

        return sorted(z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
    except Exception:  # noqa: BLE001 - no tz database (e.g. Windows without tzdata)
        return []
