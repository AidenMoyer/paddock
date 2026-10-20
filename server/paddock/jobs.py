"""Background work: scans, thumbnails, offloads, proxies. Tracked in the jobs table."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import db

log = logging.getLogger("paddock.jobs")

_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="paddock-job")
_running: dict[str, int] = {}
_lock = threading.Lock()


class JobContext:
    def __init__(self, job_id: int):
        self.id = job_id
        self._last = 0.0

    def progress(self, fraction: float, message: str | None = None) -> None:
        now = time.time()
        if now - self._last < 0.5 and fraction < 1:
            return
        self._last = now
        db.x("UPDATE jobs SET progress=?, message=COALESCE(?, message) WHERE id=?",
             (max(0.0, min(1.0, fraction)), message, self.id))


def submit(kind: str, target: str | None, fn: Callable[[JobContext], object], dedupe: bool = True) -> int:
    """Queue fn(ctx). With dedupe, an identical (kind, target) job already queued/running is reused."""
    key = f"{kind}:{target}"
    with _lock:
        if dedupe and key in _running:
            return _running[key]
        cur = db.x("INSERT INTO jobs(kind, target, state, created_at) VALUES(?,?, 'queued', ?)",
                   (kind, target, time.time()))
        job_id = cur.lastrowid
        _running[key] = job_id

    def wrapper():
        ctx = JobContext(job_id)
        db.x("UPDATE jobs SET state='running' WHERE id=?", (job_id,))
        try:
            result = fn(ctx)
            msg = result if isinstance(result, str) else None
            db.x("UPDATE jobs SET state='done', progress=1, finished_at=?, message=COALESCE(?, message) WHERE id=?",
                 (time.time(), msg, job_id))
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            log.error("job %s failed: %s", key, traceback.format_exc())
            db.x("UPDATE jobs SET state='failed', finished_at=?, message=? WHERE id=?",
                 (time.time(), f"{type(e).__name__}: {e}"[:500], job_id))
        finally:
            with _lock:
                _running.pop(key, None)
            db.close_thread_conn()

    _pool.submit(wrapper)
    return job_id


def recent(limit: int = 30) -> list[dict]:
    return db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))


def active() -> list[dict]:
    return db.q("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY id")


def mark_interrupted() -> None:
    db.x("UPDATE jobs SET state='failed', message='Interrupted by restart' WHERE state IN ('queued','running')")


class Scheduler(threading.Thread):
    """Periodic rescans and inbox filing. Cheap when nothing changed (stat-only)."""

    def __init__(self, interval_s: int = 900, inbox_interval_s: int = 60):
        super().__init__(daemon=True, name="paddock-scheduler")
        self.interval = interval_s
        self.inbox_interval = inbox_interval_s
        self.stop = threading.Event()

    def run(self) -> None:
        from . import backup, scanner

        last_full = 0.0
        while not self.stop.wait(self.inbox_interval):
            try:
                scanner.file_all_inboxes()
                if backup.due():
                    submit("backup", "all", lambda ctx: backup.run_all(ctx))
                if time.time() - last_full > self.interval:
                    last_full = time.time()
                    for lib in db.q("SELECT id FROM libraries"):
                        scanner.queue_scan(lib["id"])
            except Exception:
                log.exception("scheduler tick failed")
