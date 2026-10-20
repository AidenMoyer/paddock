"""Credentials: password hashing, opaque bearer tokens, brute-force throttling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict

# scrypt N=2^16, r=8, p=1 → 64 MiB per hash. Logins are rare; make them expensive.
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024

MIN_PASSWORD_LEN = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=_SCRYPT_MAXMEM, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p),
                            maxmem=_SCRYPT_MAXMEM, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# A pre-computed hash so that "unknown user" takes as long as "wrong password".
_DUMMY_HASH: str | None = None


def dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("paddock-timing-equalizer")
    verify_password(password, _DUMMY_HASH)


def password_problems(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Use at least {MIN_PASSWORD_LEN} characters."
    if password.lower() in {"password123", "paddock123", "letmein1234", "qwertyuiop"}:
        return "That password is too common."
    return None


def new_token(prefix: str = "pdk") -> str:
    """256-bit random bearer token. The prefix makes leaked tokens greppable by secret scanners."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Throttle:
    """Exponential back-off per key (IP and username) after repeated failures."""

    def __init__(self, free_attempts: int = 5, base_delay: float = 2.0, max_delay: float = 900.0):
        self.free = free_attempts
        self.base = base_delay
        self.max = max_delay
        self._fails: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def retry_after(self, *keys: str) -> float:
        now = time.time()
        worst = 0.0
        with self._lock:
            for k in keys:
                fails = [t for t in self._fails.get(k, []) if now - t < 3600]
                self._fails[k] = fails
                if len(fails) >= self.free:
                    delay = min(self.base * 2 ** (len(fails) - self.free), self.max)
                    wait = fails[-1] + delay - now
                    worst = max(worst, wait)
        return max(worst, 0.0)

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._fails[k].append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


login_throttle = Throttle()
pairing_throttle = Throttle(free_attempts=3, base_delay=10.0)
