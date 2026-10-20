"""Runtime configuration, read once from environment variables.

Everything that touches the network or the filesystem is configured here so the
TrueNAS app definition (deploy/compose.yaml) is the single place an operator edits.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"PADDOCK_{name}", default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str, default: str, sep: str = ",") -> list[str]:
    return [p.strip() for p in _env(name, default).split(sep) if p.strip()]


# RFC1918 + CGNAT-less private ranges, link-local, and IPv6 ULA. These are the
# only source networks that may talk to the API unless the operator says otherwise.
DEFAULT_LAN_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fd00::/8,fe80::/10"


@dataclass
class Settings:
    data_dir: Path
    media_roots: list[Path]
    host: str
    port: int
    tls: bool
    hostnames: list[str]
    lan_cidrs: list[ipaddress._BaseNetwork]
    trust_loopback: bool
    wg_enabled: bool
    wg_endpoint: str
    wg_subnet: ipaddress.IPv4Network
    wg_port: int
    hwaccel: str
    redline: str
    ffmpeg: str
    ffprobe: str
    session_days: int
    allow_remote_pairing: bool
    dev_mode: bool
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "paddock.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def thumbs_dir(self) -> Path:
        return self.cache_dir / "thumbs"

    @property
    def transcode_dir(self) -> Path:
        return self.cache_dir / "transcode"

    @property
    def uploads_tmp_dir(self) -> Path:
        return self.data_dir / "uploads-incoming"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def wg_dir(self) -> Path:
        return self.data_dir / "wireguard"

    @property
    def wg_server_ip(self) -> str:
        return str(next(self.wg_subnet.hosts()))

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.cache_dir,
            self.thumbs_dir,
            self.transcode_dir,
            self.uploads_tmp_dir,
            self.tls_dir,
            self.wg_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "./data")).resolve()
    roots = []
    for p in _env_list("MEDIA_ROOTS", "./media", sep=os.pathsep if os.name == "nt" else ":"):
        try:
            roots.append(Path(p).resolve())
        except OSError as e:  # e.g. a network drive that needs a login — skip it, don't refuse to start
            import logging

            logging.getLogger("paddock").warning("Skipping media root %s: %s", p, e)
    return Settings(
        data_dir=data_dir,
        media_roots=roots,
        host=_env("HOST", "0.0.0.0"),
        port=int(_env("PORT", "8443")),
        tls=_env_bool("TLS", True),
        hostnames=_env_list("HOSTNAMES", "paddock.local,localhost"),
        lan_cidrs=[ipaddress.ip_network(c, strict=False) for c in _env_list("LAN_CIDRS", DEFAULT_LAN_CIDRS)],
        trust_loopback=_env_bool("TRUST_LOOPBACK", True),
        wg_enabled=_env_bool("WG_ENABLED", False),
        wg_endpoint=_env("WG_ENDPOINT", ""),
        wg_subnet=ipaddress.IPv4Network(_env("WG_SUBNET", "10.77.0.0/24")),
        wg_port=int(_env("WG_PORT", "51820")),
        hwaccel=_env("HWACCEL", "auto").lower(),
        redline=_env("REDLINE", "REDline"),
        ffmpeg=_env("FFMPEG", "ffmpeg"),
        ffprobe=_env("FFPROBE", "ffprobe"),
        session_days=int(_env("SESSION_DAYS", "30")),
        allow_remote_pairing=_env_bool("ALLOW_REMOTE_PAIRING", False),
        dev_mode=_env_bool("DEV", False),
    )


settings: Settings = load_settings()


def reload_settings() -> Settings:
    """Re-read the environment (used by tests that point PADDOCK_* at temp dirs)."""
    global settings
    new = load_settings()
    settings.__dict__.update(new.__dict__)
    return settings
