import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(tempfile.mkdtemp(prefix="paddock-test-"))
os.environ.update({
    "PADDOCK_DATA_DIR": str(ROOT / "data"),
    "PADDOCK_MEDIA_ROOTS": str(ROOT / "media"),
    "PADDOCK_TLS": "0",
    "PADDOCK_DEV": "1",
    "PADDOCK_WG_ENABLED": "1",
    "PADDOCK_WG_ENDPOINT": "home.example.net:51820",
    "PADDOCK_WG_SUBNET": "10.77.0.0/24",
    "PADDOCK_HWACCEL": "none",   # deterministic: tests mustn't depend on the machine's GPUs
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from paddock.config import reload_settings  # noqa: E402

reload_settings()

import media_fixtures  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from paddock import db, jobs  # noqa: E402
from paddock.app import create_app  # noqa: E402

LAN = ("192.168.1.50", 50000)
PASSWORD = "correct horse battery"


@pytest.fixture(scope="session")
def libs():
    return media_fixtures.build(ROOT / "media")


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture(scope="session")
def admin(app, libs):
    """Signed-in admin client (web cookie) on the home LAN, with every library scanned."""
    c = TestClient(app, client=LAN, headers={"x-paddock": "1"})
    c.__enter__()
    r = c.post("/api/auth/setup", json={"username": "aiden", "password": PASSWORD, "server_name": "Pit Lane"})
    assert r.status_code == 200, r.text
    for kind, path in libs.items():
        r = c.post("/api/libraries", json={"name": kind.title(), "kind": kind, "path": str(path)})
        assert r.status_code == 200, r.text
    wait_jobs()
    yield c
    c.__exit__(None, None, None)
    shutil.rmtree(ROOT, ignore_errors=True)


def wait_jobs(timeout: float = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not jobs.active():
            return
        time.sleep(0.2)
    raise AssertionError(f"jobs still running: {jobs.active()}")


def client(app, ip="192.168.1.77", **kw) -> TestClient:
    return TestClient(app, client=(ip, 50001), **kw)


def item_by_title(c, title: str) -> dict:
    items = c.get("/api/items", params={"limit": 500}).json()
    for i in items:
        if i["title"] == title:
            return i
    raise AssertionError(f"{title!r} not in {[i['title'] for i in items]}")
