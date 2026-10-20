"""Authentication, network policy, CSRF, pairing, tokens, path safety."""

import time

from conftest import LAN, PASSWORD, client

from paddock import db, wireguard
from paddock.security import login_throttle


def test_internet_addresses_are_refused(app, admin):
    c = client(app, ip="8.8.8.8")
    assert c.get("/api/health").status_code == 403
    assert c.get("/").status_code == 403
    assert c.post("/mcp", json={}).status_code == 403


def test_tunnel_and_lan_allowed(app, admin):
    assert client(app, ip="10.77.0.9").get("/api/health").status_code == 200
    assert client(app, ip="192.168.1.20").get("/api/health").status_code == 200


def test_setup_only_once(app, admin):
    r = client(app).post("/api/auth/setup", json={"username": "evil", "password": "x" * 20})
    assert r.status_code == 409


def test_unauthenticated_is_rejected(app, admin):
    c = client(app)
    for path in ("/api/home", "/api/items", "/api/libraries", "/api/admin/overview", "/api/items/1/file"):
        assert c.get(path).status_code == 401, path


def test_login_wrong_password_then_throttle(app, admin):
    login_throttle.reset("ip:192.168.1.66", "user:aiden")
    c = client(app, ip="192.168.1.66")
    codes = [c.post("/api/auth/login", json={"username": "aiden", "password": "nope"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5
    assert 429 in codes[5:]
    login_throttle.reset("ip:192.168.1.66", "user:aiden")


def test_unknown_user_same_error(app, admin):
    login_throttle.reset("ip:192.168.1.67", "user:ghost")
    r = client(app, ip="192.168.1.67").post("/api/auth/login", json={"username": "ghost", "password": "whatever123"})
    assert r.status_code == 401 and r.json()["detail"] == "Wrong username or password"


def test_cookie_writes_need_antiforgery_header(app, admin):
    c = client(app)
    r = c.post("/api/auth/login", json={"username": "aiden", "password": PASSWORD})
    assert r.status_code == 200
    # cookie session, no x-paddock header → refused
    r = c.post("/api/projects", json={"name": "csrf"})
    assert r.status_code == 403
    r = c.post("/api/projects", json={"name": "ok"}, headers={"x-paddock": "1"})
    assert r.status_code == 200


def test_tokens_stored_hashed(admin):
    rows = db.q("SELECT token_hash FROM sessions")
    assert rows and all(len(r["token_hash"]) == 64 and not r["token_hash"].startswith("pdk") for r in rows)


def test_pairing_flow_and_revocation(app, admin):
    r = admin.post("/api/devices/pairing")
    assert r.status_code == 200
    payload = r.json()["payload"]
    assert payload["code"] and payload["wg"] is True and "<svg" in r.json()["qr_svg"]
    phone = client(app, ip="192.168.1.123")
    import base64, os

    pub = base64.b64encode(os.urandom(32)).decode()
    r = phone.post("/api/pair", json={"code": payload["code"], "name": "Pixel 9", "wg_public_key": pub})
    assert r.status_code == 200, r.text
    body = r.json()
    tok = body["token"]
    assert body["wireguard"]["allowed_ips"] == "10.77.0.1/32"  # tunnel reaches Paddock only
    assert body["wireguard"]["address"].startswith("10.77.0.")
    # code is single-use
    assert phone.post("/api/pair", json={"code": payload["code"], "name": "again"}).status_code == 401
    # peer written to the managed WireGuard config
    conf = (wireguard.settings.wg_dir / "wg0.conf").read_text()
    assert pub in conf and "AllowedIPs = 10.77.0." in conf
    # device token works (from the tunnel, too)
    remote = client(app, ip=body["wireguard"]["address"].split("/")[0])
    assert remote.get("/api/home", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    # revoke → token dead, peer removed
    dev_id = body["device_id"]
    assert admin.delete(f"/api/devices/{dev_id}").status_code == 200
    assert remote.get("/api/home", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    assert pub not in (wireguard.settings.wg_dir / "wg0.conf").read_text()


def test_pairing_refused_from_tunnel(app, admin):
    code = admin.post("/api/devices/pairing").json()["payload"]["code"]
    r = client(app, ip="10.77.0.50").post("/api/pair", json={"code": code, "name": "x"})
    assert r.status_code == 403


def test_expired_pairing_code(app, admin):
    code = admin.post("/api/devices/pairing").json()["payload"]["code"]
    db.x("UPDATE pairing_codes SET expires_at=?", (time.time() - 1,))
    assert client(app, ip="192.168.1.124").post("/api/pair", json={"code": code, "name": "late"}).status_code == 401


def test_fs_browse_is_confined(admin, libs):
    assert admin.get("/api/fs/browse", params={"path": "C:/Windows" if __import__("os").name == "nt" else "/etc"}).status_code == 400
    r = admin.get("/api/fs/browse", params={"path": str(libs["movies"])})
    assert r.status_code == 200


def test_library_outside_roots_refused(admin, tmp_path):
    r = admin.post("/api/libraries", json={"name": "bad", "kind": "movies", "path": str(tmp_path)})
    assert r.status_code == 400


def test_sidecar_subtitle_traversal(admin):
    item = admin.get("/api/items", params={"kind": "video", "limit": 1}).json()[0]
    r = admin.get(f"/api/items/{item['id']}/subtitles/file/..%2F..%2Fpaddock.db")
    assert r.status_code == 404


def test_security_headers(admin):
    r = admin.get("/api/health")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "SAMEORIGIN"  # PDFs embed in our own pages only
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_non_admin_cannot_manage(app, admin):
    assert admin.post("/api/users", json={"username": "kid", "password": "kid-password-123"}).status_code == 200
    kid = client(app, ip="192.168.1.88", headers={"x-paddock": "1"})
    assert kid.post("/api/auth/login", json={"username": "kid", "password": "kid-password-123"}).status_code == 200
    assert kid.get("/api/home").status_code == 200
    assert kid.get("/api/admin/overview").status_code == 403
    assert kid.post("/api/tokens", json={"name": "x", "scopes": ["admin"]}).status_code == 403
    assert kid.post("/api/trash/empty", json={"ids": [1], "confirm": "DELETE"}).status_code == 403
    assert "path" not in kid.get("/api/libraries").json()[0]


def test_admin_pages_load(admin):
    """Every admin endpoint the web console calls must answer."""
    for path in ("/api/admin/overview", "/api/admin/remote", "/api/federation", "/api/admin/ai", "/api/admin/stt",
                 "/api/admin/network", "/api/admin/audit", "/api/admin/sessions", "/api/jobs", "/api/tokens", "/api/users",
                 "/api/printers", "/api/devices", "/api/share", "/api/collections", "/api/projects", "/api/places",
                 "/api/timeline", "/api/tags", "/api/stats", "/api/ai/info", "/api/library-kinds"):
        r = admin.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
