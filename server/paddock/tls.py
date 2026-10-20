"""Self-managed TLS. No public CA, no ACME, no outside dependency.

On first boot Paddock mints a private CA and a server certificate signed by it.
The phone app pins the server certificate's SHA-256 fingerprint (delivered inside
the pairing QR code), so it never trusts anything else. Browsers can either accept
the certificate once or import ca.crt to remove the warning.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import settings


def _paths() -> tuple[Path, Path, Path, Path]:
    d = settings.tls_dir
    return d / "ca.key", d / "ca.crt", d / "server.key", d / "server.crt"


def _write_key(path: Path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address used for the default route
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # TEST-NET, nothing is sent
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def ensure_certificates() -> tuple[Path, Path]:
    ca_key_p, ca_crt_p, key_p, crt_p = _paths()
    settings.tls_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)

    if ca_key_p.exists() and not _has_ext(ca_crt_p, x509.SubjectKeyIdentifier):
        ca_key_p.unlink()  # pre-release CA without key identifiers: mint a compliant one
        crt_p.unlink(missing_ok=True)
    if not ca_key_p.exists():
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Paddock Private CA")])
        ca_crt = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_key(ca_key_p, ca_key)
        ca_crt_p.write_bytes(ca_crt.public_bytes(serialization.Encoding.PEM))
        if crt_p.exists():
            crt_p.unlink()

    if not crt_p.exists() or _needs_renewal(crt_p):
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), password=None)
        ca_crt = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
        key = ec.generate_private_key(ec.SECP256R1())
        sans: list[x509.GeneralName] = []
        for h in settings.hostnames:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(h)))
            except ValueError:
                sans.append(x509.DNSName(h))
        for ip in _local_ips() + [settings.wg_server_ip]:
            ipn = x509.IPAddress(ipaddress.ip_address(ip))
            if ipn not in sans:
                sans.append(ipn)
        crt = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, settings.hostnames[0] if settings.hostnames else "paddock")]))
            .issuer_name(ca_crt.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(True, False, False, False, True, False, False, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write_key(key_p, key)
        crt_p.write_bytes(crt.public_bytes(serialization.Encoding.PEM) + ca_crt_p.read_bytes())
    return crt_p, key_p


def _has_ext(crt_p: Path, ext_type) -> bool:
    try:
        x509.load_pem_x509_certificate(crt_p.read_bytes()).extensions.get_extension_for_class(ext_type)
        return True
    except (x509.ExtensionNotFound, OSError, ValueError):
        return False


def _needs_renewal(crt_p: Path) -> bool:
    if not _has_ext(crt_p, x509.AuthorityKeyIdentifier):
        return True
    crt = x509.load_pem_x509_certificate(crt_p.read_bytes())
    remaining = crt.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
    return remaining < dt.timedelta(days=30)


def server_fingerprint() -> str | None:
    """SHA-256 of the leaf certificate (DER), hex, as the app pins it."""
    _, _, _, crt_p = _paths()
    if not crt_p.exists():
        return None
    crt = x509.load_pem_x509_certificate(crt_p.read_bytes())
    return hashlib.sha256(crt.public_bytes(serialization.Encoding.DER)).hexdigest()


def spki_pin() -> str | None:
    """Base64 SHA-256 of the CA public key (OkHttp CertificatePinner format, survives leaf renewal)."""
    import base64

    _, ca_crt_p, _, _ = _paths()
    if not ca_crt_p.exists():
        return None
    ca = x509.load_pem_x509_certificate(ca_crt_p.read_bytes())
    spki = ca.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def ca_pem() -> bytes | None:
    _, ca_crt_p, _, _ = _paths()
    return ca_crt_p.read_bytes() if ca_crt_p.exists() else None
