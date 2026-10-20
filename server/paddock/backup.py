"""Encrypted, deduplicated, incremental backups to linked servers.

Format
------
* Files are split into 8 MB chunks. A chunk's name is HMAC-SHA256(id_key, plaintext)
  so identical data is stored once, and the receiving server learns nothing from
  names. Its content is AES-256-GCM(enc_key, nonce, plaintext, aad=name).
* A snapshot manifest (JSON: libraries → files → chunk names, plus the Paddock
  database) is compressed and encrypted the same way.
* Keys are derived (HKDF) from one 32-byte master key kept on this server. The
  admin can view it as a recovery key; with it, any new Paddock can restore.

Unchanged files are never re-read (size+mtime cache) and chunks the target
already has are never re-sent, so nightly runs only move what changed.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import db, federation, jobs
from .config import settings

log = logging.getLogger("paddock.backup")

CHUNK = 8 * 1024 * 1024
MAGIC = b"PDK1"

DEFAULTS = {"enabled": False, "hour": 3, "keep": 14, "bandwidth_mbps": 0, "libraries": [], "targets": [],
            "include_database": True}


def config() -> dict:
    c = dict(DEFAULTS)
    c.update(db.get_setting("backup", {}))
    return c


def save_config(update: dict) -> dict:
    c = db.get_setting("backup", {})
    for k in DEFAULTS:
        if k in update and update[k] is not None:
            c[k] = update[k]
    c["hour"] = int(c.get("hour", 3)) % 24
    c["keep"] = max(1, min(int(c.get("keep", 14)), 365))
    db.set_setting("backup", c)
    return config()


# ---- keys --------------------------------------------------------------------------------------

def _key_path() -> Path:
    return settings.data_dir / "backup.key"


def master_key() -> bytes:
    p = _key_path()
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p.read_bytes()


def recovery_key() -> str:
    raw = base64.b32encode(master_key()).decode().rstrip("=")
    return "PDK1-" + "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def key_from_recovery(text: str) -> bytes:
    raw = text.strip().upper().removeprefix("PDK1-").replace("-", "").replace(" ", "")
    return base64.b32decode(raw + "=" * (-len(raw) % 8))


def _derive(master: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"paddock-backup-v1", info=label).derive(master)


class Crypto:
    def __init__(self, master: bytes | None = None):
        m = master or master_key()
        self.enc = AESGCM(_derive(m, b"enc"))
        self.idk = _derive(m, b"id")

    def chunk_id(self, data: bytes) -> str:
        return hmac.new(self.idk, data, hashlib.sha256).hexdigest()

    def seal(self, data: bytes, aad: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return MAGIC + nonce + self.enc.encrypt(nonce, data, aad.encode())

    def open(self, blob: bytes, aad: str) -> bytes:
        if blob[:4] != MAGIC:
            raise ValueError("not a Paddock backup object")
        return self.enc.decrypt(blob[4:16], blob[16:], aad.encode())


# ---- running a backup ----------------------------------------------------------------------------

class Throttle:
    def __init__(self, mbps: float):
        self.rate = mbps * 125_000 if mbps else 0
        self.t0 = time.time()
        self.sent = 0

    def add(self, n: int) -> None:
        if not self.rate:
            return
        self.sent += n
        ahead = self.sent / self.rate - (time.time() - self.t0)
        if ahead > 0:
            time.sleep(ahead)


class Uploader:
    def __init__(self, target_id: int, crypto: Crypto, throttle: Throttle):
        self.target = target_id
        self.client = federation.client(target_id)
        self.crypto = crypto
        self.throttle = throttle
        self.known = {r["chunk"] for r in db.q("SELECT chunk FROM backup_chunks WHERE target=?", (target_id,))}
        self.uploaded = 0

    def ensure(self, chunks: list[tuple[str, bytes]]) -> None:
        todo = [(cid, data) for cid, data in chunks if cid not in self.known]
        if not todo:
            return
        r = self.client.request("POST", "/api/peer/vault/chunks/check", json={"ids": [c for c, _ in todo]})
        r.raise_for_status()
        missing = set(r.json()["missing"])
        for cid, data in todo:
            if cid in missing:
                blob = self.crypto.seal(data, cid)
                r = self.client.request("PUT", f"/api/peer/vault/chunks/{cid}", content=blob, timeout=300,
                                        headers={"content-type": "application/octet-stream"})
                if r.status_code == 403:
                    raise PermissionError(r.json().get("detail", "quota exceeded"))
                r.raise_for_status()
                self.uploaded += len(blob)
                self.throttle.add(len(blob))
            self.known.add(cid)
            db.x("INSERT OR IGNORE INTO backup_chunks(target, chunk) VALUES(?,?)", (self.target, cid))


def _chunk_file(path: Path, crypto: Crypto):
    with path.open("rb") as f:
        while data := f.read(CHUNK):
            yield crypto.chunk_id(data), data


def _file_chunks(target: int, path: Path, rel: str, up: Uploader, crypto: Crypto) -> list[str]:
    st = path.stat()
    cached = db.q1("SELECT size, mtime, chunks FROM backup_files WHERE target=? AND path=?", (target, rel))
    if cached and cached["size"] == st.st_size and abs(cached["mtime"] - st.st_mtime) < 1:
        ids = json.loads(cached["chunks"])
        if all(c in up.known for c in ids):
            return ids
    ids: list[str] = []
    batch: list[tuple[str, bytes]] = []
    for cid, data in _chunk_file(path, crypto):
        ids.append(cid)
        batch.append((cid, data))
        if len(batch) >= 8:
            up.ensure(batch)
            batch = []
    if batch:
        up.ensure(batch)
    db.x("INSERT OR REPLACE INTO backup_files(target, path, size, mtime, chunks) VALUES(?,?,?,?,?)",
         (target, rel, st.st_size, st.st_mtime, json.dumps(ids)))
    return ids


def _db_snapshot() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "paddock.db"
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    dst.close()
    src.close()
    return tmp


def run(target_id: int, ctx: jobs.JobContext | None = None) -> str:
    cfg = config()
    crypto = Crypto()
    up = Uploader(target_id, crypto, Throttle(cfg.get("bandwidth_mbps") or 0))
    started = time.time()
    run_id = db.x("INSERT INTO backup_runs(target, started_at, state) VALUES(?,?, 'running')", (target_id, started)).lastrowid
    manifest: dict = {"v": 1, "created": started, "server": federation.self_info()["name"], "libraries": {}, "db": None}
    files = total = 0
    try:
        libs = [l for l in db.q("SELECT * FROM libraries") if l["id"] in set(cfg["libraries"])]
        all_files: list[tuple[dict, Path, str]] = []
        for lib in libs:
            root = Path(lib["path"])
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("@eaDir", "#recycle", ".zfs")]
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    p = Path(dirpath) / fn
                    all_files.append((lib, p, p.relative_to(root).as_posix()))
        grand = sum(p.stat().st_size for _, p, _ in all_files if p.exists()) or 1
        done_bytes = 0
        for lib, p, rel in all_files:
            try:
                st = p.stat()
            except OSError:
                continue
            ids = _file_chunks(target_id, p, f"{lib['id']}:{rel}", up, crypto)
            manifest["libraries"].setdefault(lib["name"], []).append({"p": rel, "s": st.st_size, "m": st.st_mtime, "c": ids})
            files += 1
            total += st.st_size
            done_bytes += st.st_size
            if ctx:
                ctx.progress(0.95 * done_bytes / grand, f"{lib['name']}: {rel}")
        if cfg.get("include_database", True):
            snap = _db_snapshot()
            try:
                ids: list[str] = []
                batch: list[tuple[str, bytes]] = []
                for cid, data in _chunk_file(snap, crypto):
                    ids.append(cid)
                    batch.append((cid, data))
                    if len(batch) >= 8:
                        up.ensure(batch)
                        batch = []
                up.ensure(batch)
                manifest["db"] = ids
            finally:
                shutil.rmtree(snap.parent, ignore_errors=True)
        name = datetime.fromtimestamp(started, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest["files"] = files
        manifest["bytes"] = total
        blob = crypto.seal(gzip.compress(json.dumps(manifest, separators=(",", ":")).encode()), f"snapshot:{name}")
        r = up.client.request("PUT", f"/api/peer/vault/snapshots/{name}", content=blob, timeout=600,
                              headers={"content-type": "application/octet-stream"})
        r.raise_for_status()
        _cache_manifest(target_id, name, manifest)
        if ctx:
            ctx.progress(0.97, "Applying retention")
        pruned = prune(target_id, cfg["keep"], crypto)
        msg = f"{files} files ({total / 1e9:.2f} GB) — sent {up.uploaded / 1e6:.1f} MB new data{f', pruned {pruned} old snapshots' if pruned else ''}"
        db.x("UPDATE backup_runs SET finished_at=?, state='done', files=?, bytes=?, uploaded=?, snapshot=?, message=? WHERE id=?",
             (time.time(), files, total, up.uploaded, name, msg, run_id))
        db.audit("system", "backup.complete", target=target_id, snapshot=name, files=files, uploaded=up.uploaded)
        return msg
    except Exception as e:
        db.x("UPDATE backup_runs SET finished_at=?, state='failed', message=? WHERE id=?", (time.time(), str(e)[:400], run_id))
        raise


# ---- manifests, retention, restore, verify ------------------------------------------------------------

def _manifest_dir(target: int) -> Path:
    return settings.data_dir / "backup-manifests" / str(target)


def _cache_manifest(target: int, name: str, manifest: dict) -> None:
    d = _manifest_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(manifest).encode()))


def load_manifest(target: int, name: str, crypto: Crypto | None = None) -> dict:
    cached = _manifest_dir(target) / f"{name}.json.gz"
    if cached.exists():
        return json.loads(gzip.decompress(cached.read_bytes()))
    crypto = crypto or Crypto()
    r = federation.client(target).request("GET", f"/api/peer/vault/snapshots/{name}", timeout=600)
    r.raise_for_status()
    manifest = json.loads(gzip.decompress(crypto.open(r.content, f"snapshot:{name}")))
    _cache_manifest(target, name, manifest)
    return manifest


def snapshots(target: int) -> list[dict]:
    r = federation.client(target).request("GET", "/api/peer/vault/snapshots", timeout=20)
    r.raise_for_status()
    out = []
    for s in r.json():
        info = {"name": s["name"], "size": s["size"],
                "created": datetime.strptime(s["name"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                if len(s["name"]) == 16 else s["created_at"]}
        cached = _manifest_dir(target) / f"{s['name']}.json.gz"
        if cached.exists():
            m = json.loads(gzip.decompress(cached.read_bytes()))
            info.update(files=m.get("files"), bytes=m.get("bytes"), libraries=list(m.get("libraries", {})))
        out.append(info)
    return sorted(out, key=lambda s: s["created"], reverse=True)


def prune(target: int, keep: int, crypto: Crypto) -> int:
    client = federation.client(target)
    snaps = sorted(s["name"] for s in client.request("GET", "/api/peer/vault/snapshots").json())
    old = snaps[:-keep] if len(snaps) > keep else []
    for name in old:
        client.request("DELETE", f"/api/peer/vault/snapshots/{name}")
        (_manifest_dir(target) / f"{name}.json.gz").unlink(missing_ok=True)
    if not old:
        return 0
    # garbage-collect chunks no remaining snapshot references (only this server can know which)
    live: set[str] = set()
    for name in snaps[-keep:]:
        m = load_manifest(target, name, crypto)
        for files in m["libraries"].values():
            for f in files:
                live.update(f["c"])
        live.update(m.get("db") or [])
    after = ""
    dead: list[str] = []
    while True:
        page = client.request("GET", f"/api/peer/vault/chunks?after={after}&limit=5000").json()
        if not page:
            break
        dead += [c for c in page if c not in live]
        after = page[-1]
    for i in range(0, len(dead), 2000):
        client.request("POST", "/api/peer/vault/chunks/delete", json={"ids": dead[i:i + 2000]})
    if dead:
        for i in range(0, len(dead), 900):
            part = dead[i:i + 900]
            db.x(f"DELETE FROM backup_chunks WHERE target=? AND chunk IN ({','.join('?' * len(part))})", (target, *part))
    return len(old)


def _fetch_chunk(client, crypto: Crypto, cid: str) -> bytes:
    r = client.request("GET", f"/api/peer/vault/chunks/{cid}", timeout=300)
    r.raise_for_status()
    data = crypto.open(r.content, cid)
    if crypto.chunk_id(data) != cid:
        raise ValueError(f"chunk {cid[:12]} failed verification")
    return data


def restore(target: int, name: str, dest: Path, library: str | None = None, prefix: str | None = None,
            ctx: jobs.JobContext | None = None, master: bytes | None = None) -> str:
    crypto = Crypto(master)
    manifest = load_manifest(target, name, crypto)
    client = federation.client(target)
    todo = []
    for lib, files in manifest["libraries"].items():
        if library and lib != library:
            continue
        for f in files:
            if prefix and not f["p"].startswith(prefix):
                continue
            todo.append((lib, f))
    total = sum(f["s"] for _, f in todo) or 1
    done = 0
    for lib, f in todo:
        out = (dest / _safe(lib) / f["p"]).resolve()
        if not str(out).startswith(str(dest.resolve())):
            continue  # never write outside the restore folder
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".partial")
        with tmp.open("wb") as fh:
            for cid in f["c"]:
                fh.write(_fetch_chunk(client, crypto, cid))
        os.replace(tmp, out)
        os.utime(out, (f["m"], f["m"]))
        done += f["s"]
        if ctx:
            ctx.progress(done / total, f"Restoring {lib}/{f['p']}")
    if manifest.get("db") and not library and not prefix:
        with (dest / "paddock-database.db").open("wb") as fh:
            for cid in manifest["db"]:
                fh.write(_fetch_chunk(client, crypto, cid))
    db.audit("system", "backup.restored", target=target, snapshot=name, files=len(todo), dest=str(dest))
    return f"Restored {len(todo)} files into {dest}"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in name).strip() or "library"


def verify(target: int, name: str, sample: float = 0.02, ctx: jobs.JobContext | None = None) -> str:
    crypto = Crypto()
    manifest = load_manifest(target, name, crypto)
    ids = sorted({c for files in manifest["libraries"].values() for f in files for c in f["c"]} | set(manifest.get("db") or []))
    if not ids:
        return "Snapshot is empty"
    pick = random.sample(ids, max(1, min(len(ids), int(len(ids) * sample) or 1, 400)))
    client = federation.client(target)
    for n, cid in enumerate(pick):
        _fetch_chunk(client, crypto, cid)
        if ctx:
            ctx.progress(n / len(pick), f"Verified {n}/{len(pick)} chunks")
    return f"Verified {len(pick)} random chunks of {len(ids)} — all decrypted and matched"


def run_all(ctx: jobs.JobContext | None = None) -> str:
    cfg = config()
    msgs = []
    for t in cfg["targets"]:
        try:
            msgs.append(run(t, ctx))
        except Exception as e:  # noqa: BLE001 - one target failing shouldn't stop the others
            msgs.append(f"target {t} failed: {e}")
    db.set_setting("backup_last", {"finished_at": time.time(), "message": " | ".join(msgs)})
    return " | ".join(msgs)


def due() -> bool:
    cfg = config()
    if not cfg["enabled"] or not cfg["targets"]:
        return False
    now = datetime.now()
    last = db.get_setting("backup_last", {}).get("finished_at", 0)
    return now.hour == cfg["hour"] and time.time() - last > 20 * 3600
