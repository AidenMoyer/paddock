"""Automatic router port mapping (NAT-PMP, then UPnP-IGD) — no outside services involved.

Paddock only ever maps ONE port: the WireGuard UDP port. The router also tells us
our public IPv4 address, which phones and linked servers use to find home, so no
"what is my IP" website or dynamic-DNS provider is needed.

Requires host networking (the default in deploy/compose.yaml) so multicast
discovery and the router are reachable.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import struct
import threading
import time
import urllib.request
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from . import db
from .config import settings

log = logging.getLogger("paddock.portmap")

LEASE = 3600
_state: dict = {"method": None, "ok": False, "external_ip": None, "gateway": None, "error": None, "updated": None}
_lock = threading.Lock()


def state() -> dict:
    return dict(_state)


def lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def default_gateway() -> str | None:
    try:  # Linux: /proc/net/route, gateway in little-endian hex
        for line in open("/proc/net/route").read().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000" and int(f[3], 16) & 2:
                return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    except OSError:
        pass
    ip = lan_ip()
    if ip:  # best guess: x.y.z.1
        parts = ip.split(".")
        return ".".join(parts[:3] + ["1"])
    return None


def is_cgnat(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a in ipaddress.ip_network("100.64.0.0/10") or a.is_private


# ---- NAT-PMP (RFC 6886) ----------------------------------------------------------------

def _natpmp(gateway: str, payload: bytes, expect_len: int) -> bytes | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        delay = 0.25
        for _ in range(4):
            s.settimeout(delay)
            s.sendto(payload, (gateway, 5351))
            try:
                data, addr = s.recvfrom(64)
                if addr[0] == gateway and len(data) >= expect_len:
                    return data
            except socket.timeout:
                delay *= 2
    finally:
        s.close()
    return None


def natpmp_map(gateway: str, port: int, lifetime: int = LEASE) -> tuple[str | None, int | None]:
    r = _natpmp(gateway, struct.pack("!BB", 0, 0), 12)
    ext_ip = None
    if r and r[1] == 128 and struct.unpack("!H", r[2:4])[0] == 0:
        ext_ip = socket.inet_ntoa(r[8:12])
    r = _natpmp(gateway, struct.pack("!BBHHHI", 0, 1, 0, port, port, lifetime), 16)
    if not r or r[1] != 129:
        return ext_ip, None
    code = struct.unpack("!H", r[2:4])[0]
    if code != 0:
        raise RuntimeError(f"NAT-PMP refused the mapping (code {code})")
    mapped = struct.unpack("!H", r[10:12])[0]
    return ext_ip, mapped


def natpmp_unmap(gateway: str, port: int) -> None:
    _natpmp(gateway, struct.pack("!BBHHHI", 0, 1, 0, port, 0, 0), 16)


# ---- UPnP IGD ------------------------------------------------------------------------------

WAN_SERVICES = ("urn:schemas-upnp-org:service:WANIPConnection:2", "urn:schemas-upnp-org:service:WANIPConnection:1",
                "urn:schemas-upnp-org:service:WANPPPConnection:1")


def _ssdp_discover(timeout: float = 2.5) -> list[str]:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
           "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    found: list[str] = []
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                loc = m.group(1).decode(errors="ignore")
                host = urlparse(loc).hostname
                # only trust descriptions served by the device that answered, on the LAN
                if host == addr[0] and ipaddress.ip_address(host).is_private and loc not in found:
                    found.append(loc)
    finally:
        s.close()
    return found


def _fetch(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - LAN router only
        return r.read(256 * 1024)


def _control_url(location: str) -> tuple[str, str] | None:
    xml = _fetch(location)
    root = ET.fromstring(xml)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    base = root.findtext("d:URLBase", default=location, namespaces=ns) or location
    for svc in root.iter("{urn:schemas-upnp-org:device-1-0}service"):
        st = svc.findtext("{urn:schemas-upnp-org:device-1-0}serviceType")
        if st in WAN_SERVICES:
            ctl = svc.findtext("{urn:schemas-upnp-org:device-1-0}controlURL")
            if ctl:
                return urljoin(base, ctl), st
    return None


def _soap(ctl: str, st: str, action: str, args: dict) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           f'<u:{action} xmlns:u="{st}">{body}</u:{action}></s:Body></s:Envelope>').encode()
    return _fetch(ctl, env, {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{st}#{action}"'}).decode(errors="ignore")


def upnp_map(port: int, internal_ip: str) -> tuple[str | None, str]:
    for loc in _ssdp_discover():
        try:
            found = _control_url(loc)
        except Exception:  # noqa: BLE001 - try the next device
            continue
        if not found:
            continue
        ctl, st = found
        ext = None
        try:
            m = re.search(r"<NewExternalIPAddress>([^<]+)<", _soap(ctl, st, "GetExternalIPAddress", {}))
            ext = m.group(1) if m else None
        except Exception:  # noqa: BLE001
            pass
        args = {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "UDP", "NewInternalPort": port,
                "NewInternalClient": internal_ip, "NewEnabled": 1, "NewPortMappingDescription": "Paddock tunnel",
                "NewLeaseDuration": LEASE}
        try:
            _soap(ctl, st, "AddPortMapping", args)
        except Exception:  # noqa: BLE001 - some routers only accept permanent leases
            args["NewLeaseDuration"] = 0
            _soap(ctl, st, "AddPortMapping", args)
        return ext, urlparse(loc).hostname or ""
    raise RuntimeError("No UPnP router answered on this network")


# ---- orchestration ------------------------------------------------------------------------

def refresh() -> dict:
    """Map (or renew) the WireGuard port. Safe to call repeatedly."""
    with _lock:
        port = settings.wg_port
        gw = default_gateway()
        errors = []
        if gw:
            try:
                ext, mapped = natpmp_map(gw, port)
                if mapped:
                    _state.update(method="NAT-PMP", ok=True, external_ip=ext, gateway=gw, error=None, updated=time.time())
                    _persist()
                    return state()
                if ext:
                    _state["external_ip"] = ext
            except Exception as e:  # noqa: BLE001
                errors.append(f"NAT-PMP: {e}")
        ip = lan_ip()
        if ip:
            try:
                ext, gw2 = upnp_map(port, ip)
                _state.update(method="UPnP", ok=True, external_ip=ext or _state.get("external_ip"), gateway=gw2,
                              error=None, updated=time.time())
                _persist()
                return state()
            except Exception as e:  # noqa: BLE001
                errors.append(f"UPnP: {e}")
        _state.update(method="NAT-PMP/UPnP", ok=False, gateway=gw, error="; ".join(errors) or "no router found",
                      updated=time.time())
        return state()


def _persist() -> None:
    if _state.get("external_ip"):
        db.set_setting("public_ip", {"ip": _state["external_ip"], "seen": time.time(), "source": _state["method"]})


def release() -> None:
    gw = default_gateway()
    if gw:
        try:
            natpmp_unmap(gw, settings.wg_port)
        except Exception:  # noqa: BLE001
            pass
    _state.update(ok=False, method=None)


def public_ip() -> str | None:
    if _state.get("external_ip"):
        return _state["external_ip"]
    rec = db.get_setting("public_ip")
    return rec.get("ip") if rec else None


class Renewer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="portmap")
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            if db.get_setting("remote", {}).get("auto_portmap") and settings.wg_enabled:
                try:
                    refresh()
                except Exception:  # noqa: BLE001
                    log.exception("port mapping failed")
            self.stop.wait(LEASE // 3)
