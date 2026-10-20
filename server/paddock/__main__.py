"""`python -m paddock` — run the server (TLS on by default)."""

from __future__ import annotations

import logging

import uvicorn

from . import db, tls, wireguard
from .app import create_app
from .config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    db.init_db()
    wireguard.init_subnet()
    kwargs = {}
    if settings.tls:
        cert, key = tls.ensure_certificates()
        kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        logging.getLogger("paddock").info("TLS fingerprint (SHA-256): %s", tls.server_fingerprint())
    uvicorn.run(create_app(), host=settings.host, port=settings.port, proxy_headers=False, server_header=False,
                date_header=False, timeout_keep_alive=30, log_level="info", **kwargs)


if __name__ == "__main__":
    main()
