"""Public links: password-protected videos on the internet, and the ACME client behind their certificate."""

import base64
import datetime
import json
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from paddock import acme, db, public

DOMAIN = "videos.example.com"
PASSWORD = "orbit-maple-42"


@pytest.fixture()
def site(admin, monkeypatch):
    """Public links on for DOMAIN, without opening real ports, mapping the router or asking Let's Encrypt."""
    monkeypatch.setattr(public.site, "apply", lambda: None)
    monkeypatch.setattr("paddock.api.public_links._queue_certificate", lambda: 0)
    monkeypatch.setattr(public.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    r = admin.put("/api/admin/public", json={"enabled": True, "domain": DOMAIN, "agreed": True, "max_viewers": 4})
    assert r.status_code == 200, r.text
    public.limiter.__init__()
    public.viewers.__init__()
    yield
    db.set_setting(public.SETTING, {})


def visitor(ip="203.0.113.7"):
    return TestClient(public.create_public_app(), base_url=f"https://{DOMAIN}", client=(ip, 40000))


def video_id():
    return db.q1("SELECT id FROM items WHERE kind='video' AND trashed_at IS NULL ORDER BY id LIMIT 1")["id"]


def test_public_settings_are_checked(admin, monkeypatch):
    monkeypatch.setattr(public.site, "apply", lambda: None)
    assert admin.put("/api/admin/public", json={"enabled": True, "domain": "paddock.local", "agreed": True}).status_code == 400
    assert admin.put("/api/admin/public", json={"enabled": True, "domain": "192.168.1.5", "agreed": True}).status_code == 400
    r = admin.put("/api/admin/public", json={"enabled": True, "domain": DOMAIN, "agreed": False})
    assert r.status_code == 400 and "Subscriber Agreement" in r.json()["detail"]
    # links need public links turned on
    db.set_setting(public.SETTING, {})
    assert admin.post("/api/public-links", json={"item_id": video_id(), "password": PASSWORD}).status_code == 400


def test_watching_needs_the_password(admin, site):
    r = admin.post("/api/public-links", json={"item_id": video_id(), "password": "short"})
    assert r.status_code == 422
    link = admin.post("/api/public-links", json={"item_id": video_id(), "password": PASSWORD, "days": 7, "label": "From Aiden"}).json()
    assert link["url"].startswith(f"https://{DOMAIN}/v/") and link["state"] == "active"
    path = link["url"].split(DOMAIN)[1]

    v = visitor()
    page = v.get(path)
    assert page.status_code == 200 and "Enter the password" in page.text and "From Aiden" not in page.text
    assert page.headers["referrer-policy"] == "no-referrer" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert v.get(f"{path}/hls/master.m3u8").status_code == 404           # nothing streams before unlocking

    bad = v.post(path, data={"password": "nope-nope"})
    assert bad.status_code == 401 and "isn't right" in bad.text
    ok = v.post(path, data={"password": PASSWORD}, follow_redirects=False)
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and f"Path={path}" in cookie
    player = v.get(path)
    assert "<video" in player.text and "From Aiden" in player.text and "Download" not in player.text
    master = v.get(f"{path}/hls/master.m3u8")
    assert master.status_code == 200 and "#EXTM3U" in master.text and "2160p" not in master.text
    first = master.text.splitlines()[4]
    assert v.get(f"{path}/hls/{first}").text.startswith("#EXTM3U")
    assert v.get(f"{path}/download").status_code == 404                  # downloads are off unless allowed
    assert v.get("/api/items").status_code == 404                        # nothing of the main app is here
    assert v.get("/").status_code == 404

    # someone else's cookie for another link, or a wrong host, gets nothing
    other = TestClient(public.create_public_app(), base_url="https://evil.example.net", client=("203.0.113.8", 1))
    assert other.get(path).status_code == 404

    # a new password signs everyone out; turning the link off stops it
    assert admin.put(f"/api/public-links/{link['id']}/password", json={"password": "new-password-9"}).status_code == 200
    assert v.get(f"{path}/hls/master.m3u8").status_code == 404
    assert v.post(path, data={"password": "new-password-9"}, follow_redirects=False).status_code == 303
    assert v.get(f"{path}/hls/master.m3u8").status_code == 200
    assert admin.delete(f"/api/public-links/{link['id']}").status_code == 200
    assert v.get(f"{path}/hls/master.m3u8").status_code == 404
    ended = v.post(path, data={"password": "new-password-9"})
    assert ended.status_code == 410                                      # only the right password learns it ended
    assert v.post(path, data={"password": "wrong-guess"}).status_code == 401
    entry = db.q1("SELECT * FROM audit_log WHERE action='public.watched' ORDER BY id DESC LIMIT 1")
    assert entry and "203.0.113.7" in entry["detail"]


def test_unknown_links_look_like_wrong_passwords(site):
    v = visitor()
    page = v.get("/v/not-a-real-token")
    assert page.status_code == 200 and "Enter the password" in page.text
    r = v.post("/v/not-a-real-token", data={"password": PASSWORD})
    assert r.status_code == 401 and "isn't right" in r.text


def test_guessing_is_slowed_down(admin, site):
    link = admin.post("/api/public-links", json={"item_id": video_id(), "password": PASSWORD}).json()
    path = link["url"].split(DOMAIN)[1]
    v = visitor("198.51.100.1")
    for _ in range(public.Limiter.IP_MAX):
        assert v.post(path, data={"password": "guess-guess"}).status_code == 401
    r = v.post(path, data={"password": PASSWORD})
    assert r.status_code == 429 and "Too many" in r.text                  # even the right password waits
    assert visitor("198.51.100.2").post(path, data={"password": PASSWORD}, follow_redirects=False).status_code == 303

    # many addresses guessing one link lock the link itself for everyone
    public.limiter.__init__()
    for i in range(public.Limiter.LINK_MAX):
        visitor(f"192.0.2.{i + 1}").post(path, data={"password": "guess-guess"})
    assert visitor("192.0.2.200").post(path, data={"password": PASSWORD}).status_code == 429
    assert admin.get("/api/public-links").json()[0]["state"] == "locked"
    assert db.q1("SELECT 1 FROM audit_log WHERE action='public.link_locked'")


def test_viewer_limit_and_expiry(admin, site):
    admin.put("/api/admin/public", json={"enabled": True, "domain": DOMAIN, "agreed": True, "max_viewers": 1})
    link = admin.post("/api/public-links", json={"item_id": video_id(), "password": PASSWORD, "days": 1}).json()
    path = link["url"].split(DOMAIN)[1]
    a, b = visitor("203.0.113.20"), visitor("203.0.113.21")
    for v in (a, b):
        v.post(path, data={"password": PASSWORD}, follow_redirects=False)
    assert a.get(f"{path}/hls/master.m3u8").status_code == 200
    busy = b.get(f"{path}/hls/master.m3u8")
    assert busy.status_code == 503 and "Too many people" in busy.text
    db.x("UPDATE public_links SET expires_at=? WHERE id=?", (time.time() - 1, link["id"]))
    assert a.get(f"{path}/hls/master.m3u8").status_code == 404
    assert admin.get("/api/public-links").json()[0]["state"] == "expired"


def test_challenge_listener_only_answers_challenges(site):
    c = TestClient(public.create_challenge_app(), base_url=f"http://{DOMAIN}")
    public._challenges["tok"] = "tok.thumb"
    assert c.get("/.well-known/acme-challenge/tok").text == "tok.thumb"
    assert c.get("/.well-known/acme-challenge/other").status_code == 404
    r = c.get("/v/abc", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"https://{DOMAIN}/v/abc"
    assert c.get("/admin", follow_redirects=False).status_code == 404
    public._challenges.clear()


# ---- ACME ------------------------------------------------------------------------------------

class FakeAcme:
    """Just enough of RFC 8555 to check that every request is a correctly signed JWS."""

    def __init__(self, published: dict):
        self.base = "https://acme.test"
        self.published = published
        self.accounts: dict[str, ec.EllipticCurvePublicKey] = {}
        self.jwks: dict[str, dict] = {}
        self.authz_valid = False
        self.nonces = 0
        self.bad_nonce_once = True
        self.cert_pem = b""

    def nonce(self) -> str:
        self.nonces += 1
        return f"n{self.nonces}"

    def verify(self, request: httpx.Request) -> tuple[dict, dict | None]:
        jws = json.loads(request.content)
        protected = json.loads(base64.urlsafe_b64decode(jws["protected"] + "=="))
        assert protected["alg"] == "ES256" and protected["url"] == str(request.url) and protected["nonce"].startswith("n")
        jwk = protected.get("jwk") or self.jwks[protected["kid"]]
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=="), "big"),
                                            int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=="), "big"), ec.SECP256R1()).public_key()
        sig = base64.urlsafe_b64decode(jws["signature"] + "==")
        assert len(sig) == 64
        pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
                   f"{jws['protected']}.{jws['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
        payload = json.loads(base64.urlsafe_b64decode(jws["payload"] + "==")) if jws["payload"] else None
        return protected | {"_jwk": jwk}, payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        b, path = self.base, request.url.path
        h = {"Replay-Nonce": self.nonce()}
        if path == "/dir":
            return httpx.Response(200, json={"newNonce": f"{b}/nonce", "newAccount": f"{b}/acct", "newOrder": f"{b}/order"})
        if path == "/nonce":
            return httpx.Response(200, headers=h)
        prot, payload = self.verify(request)
        if path == "/acct":
            assert payload["termsOfServiceAgreed"] is True and payload["contact"] == ["mailto:me@example.com"]
            self.jwks[f"{b}/acct/1"] = prot["_jwk"]
            return httpx.Response(201, json={"status": "valid"}, headers=h | {"Location": f"{b}/acct/1"})
        assert prot["kid"] == f"{b}/acct/1"                              # everything after newAccount uses the kid
        if path == "/order":
            if self.bad_nonce_once:
                self.bad_nonce_once = False
                return httpx.Response(400, json={"type": "urn:ietf:params:acme:error:badNonce"}, headers=h)
            assert payload["identifiers"] == [{"type": "dns", "value": DOMAIN}]
            return httpx.Response(201, json={"status": "pending", "authorizations": [f"{b}/authz/1"], "finalize": f"{b}/finalize/1"},
                                  headers=h | {"Location": f"{b}/order/1"})
        if path == "/authz/1":
            assert payload is None                                       # POST-as-GET
            return httpx.Response(200, json={"status": "valid" if self.authz_valid else "pending",
                                             "challenges": [{"type": "http-01", "url": f"{b}/chall/1", "token": "tok123"}]}, headers=h)
        if path == "/chall/1":
            thumb = acme.b64u(__import__("hashlib").sha256(json.dumps(prot["_jwk"], sort_keys=True, separators=(",", ":")).encode()).digest())
            assert self.published.get("tok123") == f"tok123.{thumb}"
            self.authz_valid = True
            return httpx.Response(200, json={"status": "processing"}, headers=h)
        if path == "/finalize/1":
            csr = x509.load_der_x509_csr(base64.urlsafe_b64decode(payload["csr"] + "=="))
            assert csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == [DOMAIN]
            ca = ec.generate_private_key(ec.SECP256R1())
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake CA")]))
                    .public_key(csr.public_key()).serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=90))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName(DOMAIN)]), critical=False).sign(ca, hashes.SHA256()))
            self.cert_pem = cert.public_bytes(serialization.Encoding.PEM)
            return httpx.Response(200, json={"status": "valid", "certificate": f"{b}/cert/1"}, headers=h)
        if path == "/cert/1":
            assert request.headers["accept"] == "application/pem-certificate-chain"
            return httpx.Response(200, content=self.cert_pem, headers=h)
        return httpx.Response(404, headers=h)


def test_acme_client_gets_a_certificate():
    published: dict[str, str] = {}
    server = FakeAcme(published)
    http = httpx.Client(transport=httpx.MockTransport(server.handler))
    client = acme.Client("https://acme.test/dir", acme.new_key(), http)
    client.account("me@example.com")
    key = acme.new_key()
    pem = client.issue(DOMAIN, key, published.__setitem__, lambda t: published.pop(t, None))
    assert acme.certificate_names(pem) == [DOMAIN] and 89 < acme.expires_in_days(pem) <= 90
    assert published == {}                                               # the challenge is taken down again
    assert x509.load_pem_x509_certificate(pem).public_key().public_numbers() == key.public_key().public_numbers()
