"""MCP server, scoped tokens, built-in assistant tool loop, uploads, projects, sharing, trash."""

import hashlib
import json

from conftest import LAN, client, item_by_title, wait_jobs

from paddock import ai, db


def _mcp(c, token, method, params=None, id_=1):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                  headers={"Authorization": f"Bearer {token}"})


def test_mcp_read_only_token(app, admin):
    tok = admin.post("/api/tokens", json={"name": "openclaw-read", "scopes": ["read"]}).json()["token"]
    assert tok.startswith("pdk_ai_")
    c = client(app)
    r = _mcp(c, tok, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "openclaw"}})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "paddock"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 202
    names = {t["name"] for t in _mcp(c, tok, "tools/list").json()["result"]["tools"]}
    assert "search_media" in names and "add_tags" not in names and "list_devices" not in names
    out = _mcp(c, tok, "tools/call", {"name": "search_media", "arguments": {"query": "matrix"}}).json()["result"]
    assert not out["isError"] and "The Matrix" in out["content"][0]["text"]
    denied = _mcp(c, tok, "tools/call", {"name": "add_tags", "arguments": {"item_ids": [1], "tags": ["x"]}}).json()
    assert "error" in denied
    bad = _mcp(c, tok, "tools/call", {"name": "search_media", "arguments": {"bogus": 1}}).json()["result"]
    assert bad["isError"]


def test_mcp_write_token_and_audit(app, admin):
    tok = admin.post("/api/tokens", json={"name": "openclaw", "scopes": ["read", "write"]}).json()["token"]
    c = client(app)
    m = item_by_title(admin, "The Matrix")
    r = _mcp(c, tok, "tools/call", {"name": "add_tags", "arguments": {"item_ids": [m["id"]], "tags": ["classic"]}})
    assert not r.json()["result"]["isError"]
    assert "classic" in admin.get(f"/api/items/{m['id']}").json()["tags"]
    log = db.q("SELECT actor, action FROM audit_log WHERE action='tool.add_tags'")
    assert log and log[-1]["actor"] == "agent:openclaw"
    # agents can trash (reversibly) but never permanently delete
    r = _mcp(c, tok, "tools/call", {"name": "move_to_trash", "arguments": {"item_ids": [m["id"]], "reason": "test"}})
    assert not r.json()["result"]["isError"]
    assert c.post("/api/trash/empty", json={"ids": [m["id"]], "confirm": "DELETE"},
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    _mcp(c, tok, "tools/call", {"name": "restore_from_trash", "arguments": {"item_ids": [m["id"]]}})
    assert admin.get(f"/api/items/{m['id']}").status_code == 200


def test_mcp_requires_token(app, admin):
    assert client(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_assistant_without_model(admin):
    r = admin.post("/api/ai/chat", json={"message": "hi"})
    assert r.status_code == 409


def test_assistant_tool_loop_with_local_model(admin, monkeypatch):
    """Simulate Ollama/Qwen3.8 calling a tool, then answering."""
    ai.save_config({"provider": "ollama", "model": "qwen3.8:27b"})
    seen = []

    def fake_chat(cfg, messages, tool_specs=None, model=None, think=None, timeout=600, num_ctx=None):
        seen.append({"think": think, "num_ctx": num_ctx or cfg["num_ctx"], "tools": [t["function"]["name"] for t in tool_specs or []]})
        if messages[-1]["role"] == "user":
            return {"message": {"role": "assistant", "content": "", "thinking": "search first",
                                "tool_calls": [{"function": {"name": "search_media", "arguments": {"query": "garage"}}}]}}
        assert messages[-1]["role"] == "tool" and "Turbo Time" in messages[-1]["content"]
        return {"message": {"role": "assistant", "content": "Found **Turbo Time** (#2)."}}

    monkeypatch.setattr(ai, "_ollama_chat", fake_chat)
    try:
        r = admin.post("/api/ai/chat", json={"message": "find the garage episodes"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Turbo Time" in body["reply"] and body["steps"][0]["tool"] == "search_media"
        assert not body["can_write"] and "add_tags" not in seen[0]["tools"]  # read-only by default
        assert seen[0]["think"] is True and seen[0]["num_ctx"] == 32768
        # follow-up keeps the same conversation (append-only history)
        r2 = admin.post("/api/ai/chat", json={"message": "thanks", "conversation_id": body["conversation_id"]})
        assert r2.json()["conversation_id"] == body["conversation_id"]
        # opting in exposes write tools
        admin.patch("/api/me", json={"allow_ai_changes": True})
        admin.post("/api/ai/chat", json={"message": "tag it"})
        assert "add_tags" in seen[-1]["tools"]
    finally:
        admin.patch("/api/me", json={"allow_ai_changes": False})
        ai.save_config({"provider": "none"})


def test_ai_settings_never_leak_keys(admin):
    admin.put("/api/admin/ai", json={"provider": "none", "claude_api_key": "sk-ant-test-secret"})
    cfg = admin.get("/api/admin/ai").json()
    assert cfg["claude_key_set"] and "sk-ant" not in json.dumps(cfg)
    admin.put("/api/admin/ai", json={"clear_claude_api_key": True})
    assert any(p["model"] == "qwen3.8:27b" for p in cfg["presets"])


def test_resumable_upload_and_dedupe(admin):
    import media_fixtures, io, tempfile, pathlib

    tmp = pathlib.Path(tempfile.mkdtemp()) / "IMG_20260101_120000.jpg"
    media_fixtures.photo(tmp, taken="2026:01:01 12:00:00")
    data = tmp.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    assert admin.post("/api/uploads/check", json={"sha256": [sha]}).json()["have"] == []
    start = admin.post("/api/uploads", json={"filename": tmp.name, "size": len(data), "sha256": sha,
                                             "taken_at": 1767268800}).json()
    uid = start["upload_id"]
    half = len(data) // 2
    assert admin.put(f"/api/uploads/{uid}", params={"offset": 0}, content=data[:half]).json()["offset"] == half
    # wrong offset (lost ack) → server says where to resume
    r = admin.put(f"/api/uploads/{uid}", params={"offset": 0}, content=data[:10])
    assert r.status_code == 409 and r.headers["upload-offset"] == str(half)
    # resume from a fresh "start" call
    again = admin.post("/api/uploads", json={"filename": tmp.name, "size": len(data), "sha256": sha}).json()
    assert again["status"] == "resume" and again["offset"] == half
    admin.put(f"/api/uploads/{uid}", params={"offset": half}, content=data[half:])
    done = admin.post(f"/api/uploads/{uid}/complete").json()
    assert done["status"] == "done" and done["path"].startswith("Uploads/2026/2026-01/")
    assert admin.post("/api/uploads/check", json={"sha256": [sha]}).json()["have"] == [sha]
    dup = admin.post("/api/uploads", json={"filename": "x.jpg", "size": len(data), "sha256": sha}).json()
    assert dup["status"] == "duplicate" and dup["item_id"] == done["item_id"]


def test_upload_checksum_mismatch(admin):
    data = b"\xff\xd8" + b"x" * 1000
    up = admin.post("/api/uploads", json={"filename": "bad.jpg", "size": len(data), "sha256": "0" * 64}).json()
    admin.put(f"/api/uploads/{up['upload_id']}", params={"offset": 0}, content=data)
    assert admin.post(f"/api/uploads/{up['upload_id']}/complete").status_code == 422


def test_projects_plan_and_share(app, admin):
    pid = admin.post("/api/projects", json={"name": "Turbo Manifold", "goal": "Show the build start to finish"}).json()["id"]
    vids = [i["id"] for i in admin.get("/api/items", params={"kind": "video,red_clip"}).json()]
    cad = [i["id"] for i in admin.get("/api/items", params={"kind": "cad,model3d,gcode"}).json()]
    admin.post(f"/api/projects/{pid}/items", json={"item_ids": vids, "role": "footage"})
    admin.post(f"/api/projects/{pid}/items", json={"item_ids": cad, "role": "cad"})
    admin.post(f"/api/projects/{pid}/notes", json={"body": "Need a dyno pull shot"})
    p = admin.get(f"/api/projects/{pid}").json()
    assert p["summary"]["counts_by_role"]["cad"] == len(cad) and p["notes"][0]["body"].startswith("Need")
    plan = admin.post(f"/api/projects/{pid}/plan/generate", json={}).json()
    assert plan["provider"] == "template" and "## Timeline by day" in plan["plan"]
    link = admin.post("/api/share", json={"project_id": pid, "hours": 1, "max_downloads": 1}).json()
    path = link["url"].split("testserver")[-1]
    anon = client(app)
    assert anon.get(path).status_code == 200
    z = anon.get(path + "/download")
    assert z.status_code == 200 and z.content[:2] == b"PK"
    assert anon.get(path + "/download").status_code == 404  # download cap reached


def test_trash_and_permanent_delete(admin, libs):
    import media_fixtures

    f = libs["photos"] / "Camera" / "delete-me.jpg"
    media_fixtures.photo(f, taken="2022:02:02 02:02:02")
    lib = next(l for l in admin.get("/api/libraries").json() if l["kind"] == "photos")
    admin.post(f"/api/libraries/{lib['id']}/scan")
    wait_jobs()
    it = item_by_title(admin, "delete-me")
    admin.post("/api/items/trash", json={"ids": [it["id"]]})
    assert any(t["id"] == it["id"] for t in admin.get("/api/trash").json())
    assert admin.post("/api/trash/empty", json={"ids": [it["id"]], "confirm": "yes"}).status_code == 400
    assert f.exists()
    r = admin.post("/api/trash/empty", json={"ids": [it["id"]], "confirm": "DELETE"}).json()
    assert r["deleted"] == 1 and not f.exists()
