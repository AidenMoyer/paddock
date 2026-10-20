"""Manage server → System: the API, the agent protocol, and the agent's own input checks."""

import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from paddock import db, system

AGENT_PATH = Path(__file__).resolve().parents[2] / "os" / "overlay" / "usr" / "local" / "sbin" / "paddock-agent"


def load_agent():
    loader = importlib.machinery.SourceFileLoader("paddock_agent", str(AGENT_PATH))
    spec = importlib.util.spec_from_loader("paddock_agent", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_overview_without_paddock_os(admin, monkeypatch):
    monkeypatch.setenv("PADDOCK_AGENT_SOCKET", str(Path(tempfile.gettempdir()) / "no-such-agent.sock"))
    ov = admin.get("/api/admin/system").json()
    assert ov["managed"] is False
    assert ov["info"]["cores"] and any(s["label"] == "Paddock data" for s in ov["info"]["storage"])
    r = admin.get("/api/admin/system/disks")
    assert r.status_code == 400 and "Paddock OS" in r.json()["detail"]


def test_system_api_is_admin_only(app, admin):
    from conftest import client

    anon = client(app)
    assert anon.get("/api/admin/system").status_code in (401, 403)
    assert anon.post("/api/admin/system/power", json={"action": "reboot"}).status_code in (401, 403)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt", reason="Unix sockets (Paddock OS is Linux)")
def test_requests_reach_the_agent_and_are_audited(admin, monkeypatch, tmp_path):
    sock_path = str(tmp_path / "agent.sock")
    seen = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(4)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                req = json.loads(conn.makefile("rb").readline())
                seen.append(req)
                if req["op"] == "pool_create" and req["args"]["confirm"] != req["args"]["name"]:
                    resp = {"ok": False, "error": "Type media to confirm"}
                else:
                    resp = {"ok": True, "result": {"op": req["op"], "managed": True}}
                conn.sendall((json.dumps(resp) + "\n").encode())

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setenv("PADDOCK_AGENT_SOCKET", sock_path)
    try:
        assert admin.get("/api/admin/system").json()["managed"] is True
        ok = admin.post("/api/admin/system/pools", json={"name": "media", "level": "raid5",
                                                          "disks": ["/dev/sdb", "/dev/sdc", "/dev/sdd"], "confirm": "media"})
        assert ok.status_code == 200 and ok.json()["op"] == "pool_create"
        bad = admin.post("/api/admin/system/pools", json={"name": "media", "level": "raid5",
                                                           "disks": ["/dev/sdb", "/dev/sdc", "/dev/sdd"], "confirm": "nope"})
        assert bad.status_code == 400 and "confirm" in bad.json()["detail"]
        assert admin.post("/api/admin/system/pools", json={"name": "x", "level": "raid7", "disks": ["/dev/sdb", "/dev/sdc"],
                                                           "confirm": "x"}).status_code == 422
        assert seen[-1]["args"]["disks"] == ["/dev/sdb", "/dev/sdc", "/dev/sdd"]
        entry = db.q1("SELECT * FROM audit_log WHERE action='system.pool_create' ORDER BY id DESC LIMIT 1")
        assert entry and "confirm" not in (entry["detail"] or "")
    finally:
        srv.close()


def test_agent_refuses_bad_input_before_touching_anything():
    agent = load_agent()
    with pytest.raises(agent.Refused):
        agent.known_device("/dev/../etc/passwd", ("disk",))
    with pytest.raises(agent.Refused):
        agent.known_device("/dev/sda; rm -rf /", ("disk",))
    for args, why in [
        ({"name": "Bad Name", "level": "raid1", "disks": ["/dev/sdb", "/dev/sdc"], "confirm": "Bad Name"}, "name"),
        ({"name": "media", "level": "raid4", "disks": ["/dev/sdb", "/dev/sdc"], "confirm": "media"}, "raid"),
        ({"name": "media", "level": "raid5", "disks": ["/dev/sdb", "/dev/sdc"], "confirm": "media"}, "at least 3"),
        ({"name": "media", "level": "raid1", "disks": ["/dev/sdb", "/dev/sdb"], "confirm": "media"}, "different"),
        ({"name": "media", "level": "raid1", "disks": ["/dev/sdb", "/dev/sdc"], "confirm": "yes"}, "confirm"),
    ]:
        with pytest.raises(agent.Refused, match=why):
            agent.op_pool_create(args)
    with pytest.raises(agent.Refused, match="gateway"):
        agent.op_network_set({"mode": "static", "address": "192.168.1.50/24", "gateway": "10.0.0.1"})
    with pytest.raises(agent.Refused):
        agent.op_hostname({"hostname": "bad name!"})
    with pytest.raises(agent.Refused):
        agent.op_power({"action": "format-everything"})


def test_agent_protocol_rejects_unknown_operations():
    r = subprocess.run([sys.executable, str(AGENT_PATH)], input=b'{"op": "rm", "args": {}}\n', capture_output=True, timeout=30)
    assert json.loads(r.stdout) == {"ok": False, "error": "Unknown operation"}
    r = subprocess.run([sys.executable, str(AGENT_PATH)], input=b"not json\n", capture_output=True, timeout=30)
    assert json.loads(r.stdout)["ok"] is False


def test_system_call_reports_unavailable(monkeypatch):
    monkeypatch.setenv("PADDOCK_AGENT_SOCKET", "/definitely/not/here.sock")
    assert system.available() is False
    with pytest.raises(system.AgentError):
        system.call("info")
