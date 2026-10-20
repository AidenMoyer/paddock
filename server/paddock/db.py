"""SQLite storage. One file, WAL mode, a connection per thread.

The catalog is rebuildable from disk at any time (rescan), but users, devices,
progress, tags, projects and plans are not — the TrueNAS dataset holding
PADDOCK_DATA_DIR should be snapshotted like any other important dataset.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .config import settings

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT,
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    allow_ai_changes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- Bearer credentials. Only a SHA-256 of each token is stored, so a stolen
-- database cannot be replayed against the server.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- web | device
    device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL,                 -- NULL = until revoked (devices)
    last_seen REAL,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    platform TEXT,
    wg_pubkey TEXT UNIQUE,
    wg_ip TEXT UNIQUE,
    created_at REAL NOT NULL,
    last_seen REAL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);

-- Scoped tokens for AI agents / MCP clients (OpenClaw, Claude Desktop, scripts).
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,            -- JSON list: read | write | admin
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    last_used REAL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- movies | shows | music | photos | home_videos | red | engineering | mixed
    path TEXT NOT NULL UNIQUE,
    options TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    scan_state TEXT NOT NULL DEFAULT 'idle',
    last_scan REAL,
    item_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    kind TEXT NOT NULL,              -- video | audio | photo | red_clip | braw_clip | model3d | gcode | cad | pcb | document
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT,
    title TEXT NOT NULL,
    taken_at REAL,                   -- capture time (EXIF / container / R3D), falls back to mtime
    duration REAL,
    width INTEGER,
    height INTEGER,
    lat REAL,
    lon REAL,
    group_key TEXT,                  -- show / album / shoot day / part family
    sort_key TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    thumb_state TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | none | error
    added_at REAL NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0,
    trashed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_items_library ON items(library_id);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_taken ON items(taken_at);
CREATE INDEX IF NOT EXISTS idx_items_added ON items(added_at);
CREATE INDEX IF NOT EXISTS idx_items_group ON items(group_key);
CREATE INDEX IF NOT EXISTS idx_items_sha ON items(sha256);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, rel_path, tags, meta_text, tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag TEXT NOT NULL COLLATE NOCASE,
    source TEXT NOT NULL DEFAULT 'user',   -- user | auto | ai
    PRIMARY KEY (item_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position REAL NOT NULL,
    duration REAL,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, item_id)
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_by TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection_id, item_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',    -- idea | active | filming | editing | done
    location_name TEXT,
    lat REAL,
    lon REAL,
    plan TEXT,                                -- markdown video plan
    plan_updated_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS project_items (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'footage',     -- footage | broll | photo | audio | music | cad | print | reference
    note TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL,
    PRIMARY KEY (project_id, item_id)
);
CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    author TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    taken_at REAL,
    received INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',      -- open | done | failed
    item_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
    token_hash TEXT PRIMARY KEY,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    max_downloads INTEGER,
    downloads INTEGER NOT NULL DEFAULT 0,
    label TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT,
    state TEXT NOT NULL,          -- queued | running | done | failed
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
-- ---- linked servers (federation) & encrypted backup ----
CREATE TABLE IF NOT EXISTS peers (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL UNIQUE,          -- SHA-256 of their private CA certificate
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'server',   -- server | node
    ca_pem TEXT NOT NULL,                  -- their TLS is verified against this CA only
    urls TEXT NOT NULL DEFAULT '[]',       -- base URLs to try (LAN first, then tunnel)
    wg_pubkey TEXT,
    tunnel_ip TEXT,
    wg_endpoint TEXT,
    token_out TEXT NOT NULL,               -- credential we present to them
    token_in_hash TEXT NOT NULL UNIQUE,    -- SHA-256 of the credential they present to us
    our_grants TEXT NOT NULL DEFAULT '{}', -- what we give them: backup_quota_gb, libraries
    their_grants TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    last_contact REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    code_hash TEXT PRIMARY KEY,
    created_by INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    grants TEXT NOT NULL DEFAULT '{}',
    boot_pubkey TEXT,
    boot_ip TEXT
);
CREATE TABLE IF NOT EXISTS vault_objects (
    peer_id INTEGER NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                    -- chunk | snapshot
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (peer_id, kind, name)
);
CREATE TABLE IF NOT EXISTS backup_files (
    target INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    chunks TEXT NOT NULL,
    PRIMARY KEY (target, path)
);
CREATE TABLE IF NOT EXISTS backup_chunks (
    target INTEGER NOT NULL,
    chunk TEXT NOT NULL,
    PRIMARY KEY (target, chunk)
);
CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY,
    target INTEGER NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    state TEXT NOT NULL,
    files INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    uploaded INTEGER NOT NULL DEFAULT 0,
    snapshot TEXT,
    message TEXT
);
"""

_local = threading.local()
_init_lock = threading.Lock()
_initialized_path: str | None = None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    global _initialized_path
    with _init_lock:
        settings.ensure_dirs()
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        conn.close()
        _initialized_path = str(settings.db_path)


def conn() -> sqlite3.Connection:
    """Thread-local connection, (re)opened if the DB path changed (tests)."""
    c = getattr(_local, "conn", None)
    path = getattr(_local, "path", None)
    if c is None or path != str(settings.db_path):
        if _initialized_path != str(settings.db_path):
            init_db()
        c = _connect()
        _local.conn = c
        _local.path = str(settings.db_path)
    return c


def close_thread_conn() -> None:
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = conn().execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def x(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return conn().execute(sql, tuple(params))


def get_setting(key: str, default: Any = None) -> Any:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value: Any) -> None:
    x(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def audit(actor: str, action: str, **detail: Any) -> None:
    x(
        "INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
        (time.time(), actor, action, json.dumps(detail, default=str)),
    )


# ---- full-text index -------------------------------------------------------

def _meta_text(meta: dict) -> str:
    keep = []
    for k, v in meta.items():
        if isinstance(v, (str, int, float)) and k not in ("thumb", "streams"):
            keep.append(str(v))
    return " ".join(keep)


def fts_index(item_id: int) -> None:
    item = q1("SELECT id, title, rel_path, meta FROM items WHERE id=?", (item_id,))
    if not item:
        return
    tags = " ".join(r["tag"] for r in q("SELECT tag FROM tags WHERE item_id=?", (item_id,)))
    meta = json.loads(item["meta"] or "{}")
    text = _meta_text(meta)
    transcript = settings.cache_dir / "captions" / f"{item_id}.txt"
    if transcript.exists():  # words spoken in the video are searchable
        text += " " + transcript.read_text(encoding="utf-8", errors="ignore")[:500_000]
    x("DELETE FROM items_fts WHERE rowid=?", (item_id,))
    x(
        "INSERT INTO items_fts(rowid, title, rel_path, tags, meta_text) VALUES(?,?,?,?,?)",
        (item_id, item["title"], item["rel_path"].replace("/", " ").replace("_", " "), tags, text),
    )


def fts_remove(item_id: int) -> None:
    x("DELETE FROM items_fts WHERE rowid=?", (item_id,))


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 prefix query (no operators from user input)."""
    words = []
    for raw in text.replace('"', " ").split():
        w = "".join(ch for ch in raw if ch.isalnum() or ch in "-_.")
        if w:
            words.append(f'"{w}"*')
    return " ".join(words)
