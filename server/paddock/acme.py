"""A small ACME (RFC 8555) client: free certificates from Let's Encrypt for the public-links domain.

Only used when an admin turns on public links with their own domain. It proves control of the domain
with the HTTP-01 challenge (Let's Encrypt fetches http://<domain>/.well-known/acme-challenge/<token>
from Paddock's port-80 listener), then stores the certificate under data/public-tls/. Keys are
generated here and never leave the server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

log = logging.getLogger("paddock.acme")

LETSENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
LETSENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


class AcmeError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def load_or_create_key(path: Path) -> ec.EllipticCurvePrivateKey:
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return key
    key = new_key()
    write_private(path, key_pem(key))
    return key


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    tmp.replace(path)


def jwk(key: ec.EllipticCurvePrivateKey) -> dict:
    nums = key.public_key().public_numbers()
    return {"crv": "P-256", "kty": "EC", "x": b64u(nums.x.to_bytes(32, "big")), "y": b64u(nums.y.to_bytes(32, "big"))}


def thumbprint(key: ec.EllipticCurvePrivateKey) -> str:
    # RFC 7638: the required members, sorted, no whitespace
    return b64u(hashlib.sha256(json.dumps(jwk(key), sort_keys=True, separators=(",", ":")).encode()).digest())


def sign(key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")       # JWS ES256 wants raw r||s


def certificate_expiry(pem: bytes) -> float:
    cert = x509.load_pem_x509_certificate(pem)
    return cert.not_valid_after_utc.timestamp()


def certificate_names(pem: bytes) -> list[str]:
    cert = x509.load_pem_x509_certificate(pem)
    try:
        return cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return []


class Client:
    def __init__(self, directory: str, account_key: ec.EllipticCurvePrivateKey, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "Paddock-ACME"})
        self.key = account_key
        self.kid: str | None = None
        self._nonce: str | None = None
        r = self.http.get(directory)
        self._check(r)
        self.dir = r.json()

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                p = r.json()
                detail = p.get("detail") or p.get("type") or r.text
            except ValueError:
                detail = r.text[:300]
            raise AcmeError(f"Let's Encrypt: {detail}")

    def _get_nonce(self) -> str:
        if self._nonce:
            n, self._nonce = self._nonce, None
            return n
        r = self.http.head(self.dir["newNonce"])
        return r.headers["Replay-Nonce"]

    def post(self, url: str, payload: dict | None, *, with_jwk: bool = False, accept: str | None = None) -> httpx.Response:
        """A signed request; payload None means POST-as-GET. Retries once on a stale nonce."""
        for attempt in range(2):
            protected = {"alg": "ES256", "nonce": self._get_nonce(), "url": url}
            if with_jwk or not self.kid:
                protected["jwk"] = jwk(self.key)
            else:
                protected["kid"] = self.kid
            p64 = b64u(json.dumps(protected).encode())
            body64 = "" if payload is None else b64u(json.dumps(payload).encode())
            jws = {"protected": p64, "payload": body64, "signature": b64u(sign(self.key, f"{p64}.{body64}".encode()))}
            headers = {"Content-Type": "application/jose+json"}
            if accept:
                headers["Accept"] = accept
            r = self.http.post(url, content=json.dumps(jws), headers=headers)
            self._nonce = r.headers.get("Replay-Nonce")
            if r.status_code == 400 and attempt == 0 and "badNonce" in r.text:
                continue
            self._check(r)
            return r
        raise AcmeError("Let's Encrypt kept rejecting the request nonce")

    def account(self, email: str | None) -> str:
        payload: dict = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = [f"mailto:{email}"]
        r = self.post(self.dir["newAccount"], payload, with_jwk=True)
        self.kid = r.headers["Location"]
        return self.kid

    def _poll(self, url: str, done: set[str], what: str, timeout: float = 120) -> dict:
        end = time.monotonic() + timeout
        while True:
            obj = self.post(url, None).json()
            if obj.get("status") in done:
                return obj
            if obj.get("status") == "invalid":
                raise AcmeError(f"{what} failed: {_problem(obj)}")
            if time.monotonic() > end:
                raise AcmeError(f"{what} timed out (status {obj.get('status')})")
            time.sleep(2)

    def issue(self, domain: str, cert_key: ec.EllipticCurvePrivateKey,
              publish: Callable[[str, str], None], unpublish: Callable[[str], None]) -> bytes:
        """Order a certificate for `domain`; returns the PEM chain."""
        r = self.post(self.dir["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]})
        order_url, order = r.headers["Location"], r.json()
        for authz_url in order["authorizations"]:
            authz = self.post(authz_url, None).json()
            if authz.get("status") == "valid":
                continue
            ch = next((c for c in authz.get("challenges", []) if c.get("type") == "http-01"), None)
            if not ch:
                raise AcmeError("Let's Encrypt didn't offer the HTTP check for this domain")
            publish(ch["token"], f"{ch['token']}.{thumbprint(self.key)}")
            try:
                self.post(ch["url"], {})
                self._poll(authz_url, {"valid"}, "Checking that the domain points here")
            finally:
                unpublish(ch["token"])
        csr = (x509.CertificateSigningRequestBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
               .sign(cert_key, hashes.SHA256()))
        order = self.post(order["finalize"], {"csr": b64u(csr.public_bytes(serialization.Encoding.DER))}).json()
        if order.get("status") != "valid":
            order = self._poll(order_url, {"valid"}, "Issuing the certificate")
        return self.post(order["certificate"], None, accept="application/pem-certificate-chain").content


def _problem(obj: dict) -> str:
    for ch in obj.get("challenges", []):
        err = ch.get("error")
        if err:
            return err.get("detail") or err.get("type", "unknown error")
    err = obj.get("error") or {}
    return err.get("detail") or "unknown error"


def expires_in_days(pem: bytes) -> float:
    return (certificate_expiry(pem) - datetime.now(timezone.utc).timestamp()) / 86400
