"""Receiving side of backups: opaque, encrypted blobs stored for linked servers, within quota.

This server can't read anything it stores here — chunk names are keyed hashes and
contents are AES-256-GCM ciphertext produced on the sending server.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import db
from .config import settings

CHUNK_RE = re.compile(r"^[0-9a-f]{64}$")
SNAP_RE = re.compile(r"^[0-9A-Za-z_\-]{6,64}$")
MAX_CHUNK = 10 * 1024 * 1024
MAX_SNAPSHOT = 512 * 1024 * 1024


def root() -> Path:
    custom = db.get_setting("vault_dir")
    return Path(custom) if custom else settings.data_dir / "vault"


def peer_dir(peer: dict) -> Path:
    return root() / peer["node_id"][:24]


def quota_bytes(peer: dict) -> int:
    return int(float(json.loads(peer["our_grants"]).get("backup_quota_gb") or 0) * 1e9)


def used_bytes(peer_id: int) -> int:
    return db.q1("SELECT COALESCE(SUM(size),0) s FROM vault_objects WHERE peer_id=?", (peer_id,))["s"]


def _chunk_path(peer: dict, cid: str) -> Path:
    return peer_dir(peer) / "chunks" / cid[:2] / cid


def _snap_path(peer: dict, name: str) -> Path:
    return peer_dir(peer) / "snapshots" / name


def check(peer: dict, ids: list[str]) -> list[str]:
    ids = [i for i in ids if CHUNK_RE.match(i)][:2000]
    if not ids:
        return []
    have = {r["name"] for r in db.q(f"SELECT name FROM vault_objects WHERE peer_id=? AND kind='chunk' AND name IN ({','.join('?' * len(ids))})",
                                    (peer["id"], *ids))}
    return [i for i in ids if i not in have]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def put_chunk(peer: dict, cid: str, data: bytes) -> None:
    if not CHUNK_RE.match(cid):
        raise ValueError("bad chunk id")
    if len(data) > MAX_CHUNK:
        raise ValueError("chunk too large")
    if db.q1("SELECT 1 FROM vault_objects WHERE peer_id=? AND kind='chunk' AND name=?", (peer["id"], cid)):
        return
    if used_bytes(peer["id"]) + len(data) > quota_bytes(peer):
        raise PermissionError("Backup quota exceeded on this server")
    _write(_chunk_path(peer, cid), data)
    db.x("INSERT OR REPLACE INTO vault_objects(peer_id, kind, name, size, created_at) VALUES(?, 'chunk', ?, ?, ?)",
         (peer["id"], cid, len(data), time.time()))


def get_chunk(peer: dict, cid: str) -> Path:
    if not CHUNK_RE.match(cid):
        raise ValueError("bad chunk id")
    p = _chunk_path(peer, cid)
    if not p.exists():
        raise FileNotFoundError(cid)
    return p


def delete_chunks(peer: dict, ids: list[str]) -> int:
    n = 0
    for cid in ids[:5000]:
        if CHUNK_RE.match(cid):
            _chunk_path(peer, cid).unlink(missing_ok=True)
            n += db.x("DELETE FROM vault_objects WHERE peer_id=? AND kind='chunk' AND name=?", (peer["id"], cid)).rowcount
    return n


def list_chunks(peer: dict, after: str = "", limit: int = 5000) -> list[str]:
    return [r["name"] for r in db.q("SELECT name FROM vault_objects WHERE peer_id=? AND kind='chunk' AND name>? ORDER BY name LIMIT ?",
                                     (peer["id"], after, min(limit, 10000)))]


def put_snapshot(peer: dict, name: str, data: bytes) -> None:
    if not SNAP_RE.match(name):
        raise ValueError("bad snapshot name")
    if len(data) > MAX_SNAPSHOT:
        raise ValueError("snapshot too large")
    if used_bytes(peer["id"]) + len(data) > quota_bytes(peer):
        raise PermissionError("Backup quota exceeded on this server")
    _write(_snap_path(peer, name), data)
    db.x("INSERT OR REPLACE INTO vault_objects(peer_id, kind, name, size, created_at) VALUES(?, 'snapshot', ?, ?, ?)",
         (peer["id"], name, len(data), time.time()))


def list_snapshots(peer: dict) -> list[dict]:
    return db.q("SELECT name, size, created_at FROM vault_objects WHERE peer_id=? AND kind='snapshot' ORDER BY name", (peer["id"],))


def get_snapshot(peer: dict, name: str) -> Path:
    if not SNAP_RE.match(name):
        raise ValueError("bad snapshot name")
    p = _snap_path(peer, name)
    if not p.exists():
        raise FileNotFoundError(name)
    return p


def delete_snapshot(peer: dict, name: str) -> None:
    if SNAP_RE.match(name):
        _snap_path(peer, name).unlink(missing_ok=True)
        db.x("DELETE FROM vault_objects WHERE peer_id=? AND kind='snapshot' AND name=?", (peer["id"], name))


def usage(peer: dict) -> dict:
    return {"used": used_bytes(peer["id"]), "quota": quota_bytes(peer)}
