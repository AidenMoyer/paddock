"""RED (R3D) and Blackmagic RAW cinema footage.

RED cameras write one folder per clip:

    A001_0712AB.RDM/                 ← magazine (camera A, reel 001, July 12)
      A001_C003_0712AB.RDC/          ← clip 3
        A001_C003_0712AB_001.R3D     ← spans, ~4 GB each
        A001_C003_0712AB_002.R3D
        A001_C003_0712AB.RMD         ← look metadata sidecar

Paddock catalogs each RDC as ONE item and never moves or rewrites camera
originals. Rich metadata (ISO, lens, timecode, fps...) and proxies need RED's
free REDline CLI inside the container; without it the catalog still works from
the naming scheme, file system and sidecars.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import settings

RED_NAME = re.compile(r"^(?P<cam>[A-Z])(?P<reel>\d{3})_C(?P<clip>\d{3})_(?P<mm>\d{2})(?P<dd>\d{2})(?P<rand>[A-Z0-9]{2})", re.I)
RED_SPAN = re.compile(r"_(\d{3})\.r3d$", re.I)
# Blackmagic: A001_07121530_C003.braw  (camera, reel, MMDDHHMM, clip)
BRAW_NAME = re.compile(r"^(?P<cam>[A-Z])(?P<reel>\d{3})_(?P<mm>\d{2})(?P<dd>\d{2})(?P<hh>\d{2})(?P<mi>\d{2})_C(?P<clip>\d{3})", re.I)


def is_secondary_span(path: Path) -> bool:
    m = RED_SPAN.search(path.name)
    return bool(m and m.group(1) != "001")


def spans(first: Path) -> list[Path]:
    m = RED_SPAN.search(first.name)
    if not m:
        return [first]
    prefix = first.name[: m.start()]
    out = sorted(p for p in first.parent.iterdir()
                 if p.name.lower().startswith(prefix.lower()) and RED_SPAN.search(p.name))
    return out or [first]


def parse_name(path: Path) -> dict:
    name = path.name
    m = RED_NAME.match(name)
    if m:
        return {
            "camera": m["cam"].upper(), "reel": int(m["reel"]), "clip": int(m["clip"]),
            "month": int(m["mm"]), "day": int(m["dd"]),
            "clip_name": RED_SPAN.sub("", name).removesuffix(".R3D").removesuffix(".r3d"),
        }
    m = BRAW_NAME.match(name)
    if m:
        return {
            "camera": m["cam"].upper(), "reel": int(m["reel"]), "clip": int(m["clip"]),
            "month": int(m["mm"]), "day": int(m["dd"]), "hour": int(m["hh"]), "minute": int(m["mi"]),
            "clip_name": path.stem,
        }
    return {"clip_name": path.stem}


def _redline_available() -> bool:
    return shutil.which(settings.redline) is not None


def redline_meta(path: Path) -> dict:
    """Ask REDline for clip metadata. Output format varies by version, so parse tolerantly."""
    if not _redline_available():
        return {}
    try:
        r = subprocess.run([settings.redline, "--i", str(path), "--printMeta", "1"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    meta: dict[str, str] = {}
    # "Key: value" style
    for ln in lines:
        if ":" in ln and "," not in ln:
            k, v = ln.split(":", 1)
            meta[k.strip()] = v.strip()
    # CSV style: header line followed by a values line
    for i in range(len(lines) - 1):
        hdr, vals = lines[i].split(","), lines[i + 1].split(",")
        if len(hdr) > 5 and len(hdr) == len(vals):
            meta.update({h.strip(): v.strip() for h, v in zip(hdr, vals)})
            break
    return normalize_redline(meta)


_RL_KEYS = {
    "clip name": "clip_name", "camera model": "camera_model", "camera pin": "camera_pin",
    "fps": "fps", "frame width": "width", "frame height": "height", "total frames": "frames",
    "abs tc": "timecode", "absolute timecode": "timecode", "edge tc": "edge_timecode",
    "iso": "iso", "kelvin": "kelvin", "tint": "tint", "shutter (deg)": "shutter_deg",
    "shutter (1/sec)": "shutter", "lens": "lens", "lens name": "lens", "focal length": "focal_length",
    "aperture": "aperture", "redcode": "redcode", "compression": "redcode",
    "date": "date", "time": "time", "reel id": "reel_id", "camera id": "camera_id",
    "sensor name": "sensor", "color space": "color_space", "gamma space": "gamma",
}


def normalize_redline(raw: dict) -> dict:
    out: dict = {}
    for k, v in raw.items():
        key = _RL_KEYS.get(k.strip().lower())
        if key and v:
            out[key] = v
    for k in ("width", "height", "frames", "iso", "kelvin"):
        if k in out:
            try:
                out[k] = int(float(out[k]))
            except ValueError:
                out.pop(k)
    if "fps" in out:
        try:
            out["fps"] = float(out["fps"])
        except ValueError:
            out.pop("fps")
    if out.get("fps") and out.get("frames"):
        out["duration"] = out["frames"] / out["fps"]
    if out.get("date"):
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                d = datetime.strptime(out["date"], fmt)
                t = out.get("time", "00:00:00")
                hh, mm, ss = (int(x) for x in (t.split(":") + ["0", "0"])[:3])
                out["created"] = d.replace(hour=hh, minute=mm, second=ss).timestamp()
                break
            except ValueError:
                continue
    return out


def rmd_meta(clip_dir: Path) -> dict:
    """The .RMD sidecar is XML with the in-camera look. Pull a few human-useful fields."""
    out: dict = {}
    for rmd in clip_dir.glob("*.RMD"):
        try:
            text = rmd.read_text(errors="ignore")[:200_000]
        except OSError:
            continue
        for tag, key in (("iso", "iso"), ("kelvin", "kelvin"), ("tint", "tint"), ("lut", "look"),
                         ("colorspace", "color_space"), ("gammacurve", "gamma")):
            m = re.search(rf"<{tag}[^>]*>([^<]+)</{tag}>", text, re.I) or \
                re.search(rf'{tag}="([^"]+)"', text, re.I)
            if m:
                out.setdefault(key, m.group(1).strip())
        break
    return out


def find_proxy(first_span: Path, clip_name: str) -> Path | None:
    """Camera or REDline proxies that sit next to the clip or in a Proxies/ folder."""
    candidates = []
    for d in (first_span.parent, first_span.parent.parent, first_span.parent.parent / "Proxies",
              first_span.parent / "Proxies", settings.cache_dir / "red-proxies"):
        if d.is_dir():
            for ext in (".mov", ".mp4", ".MOV", ".MP4"):
                candidates += list(d.glob(f"{clip_name}*{ext}"))
    return candidates[0] if candidates else None


def make_proxy(first_span: Path, clip_name: str) -> Path | None:
    """Render a 1/4-res ProRes Proxy with REDline (if installed). Long-running."""
    if not _redline_available():
        return None
    out_dir = settings.cache_dir / "red-proxies"
    out_dir.mkdir(parents=True, exist_ok=True)
    from . import db

    args = db.get_setting("red.proxy_args", ["--format", "201", "--PRcodec", "0", "--res", "4"])
    cmd = [settings.redline, "--i", str(first_span), "--outDir", str(out_dir), "--o", clip_name, *args]
    try:
        subprocess.run(cmd, capture_output=True, timeout=6 * 3600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return find_proxy(first_span, clip_name)


def describe_clip(first_span: Path) -> dict:
    """All metadata for one RED clip (all spans)."""
    parts = spans(first_span)
    size = sum(p.stat().st_size for p in parts)
    meta = parse_name(first_span)
    meta["spans"] = len(parts)
    meta["total_size"] = size
    meta.update({k: v for k, v in rmd_meta(first_span.parent).items() if k not in meta})
    rl = redline_meta(first_span)
    meta.update(rl)
    meta["redline"] = bool(rl)
    if first_span.parent.parent.suffix.upper() == ".RDM":
        meta["magazine"] = first_span.parent.parent.name
    proxy = find_proxy(first_span, meta["clip_name"])
    if proxy:
        meta["proxy"] = str(proxy)
        if not meta.get("duration"):  # without REDline, take timing (not resolution) from the proxy
            from .probe import av_info

            av = av_info(proxy)
            if av.get("duration"):
                meta["duration"] = av["duration"]
                meta.setdefault("fps", av.get("fps"))
                meta["timing_from_proxy"] = True
                if av.get("width"):
                    meta["proxy_resolution"] = f"{av['width']}x{av['height']}"
    return meta


def braw_meta(path: Path) -> dict:
    meta = parse_name(path)
    from .probe import av_info

    meta.update({k: v for k, v in av_info(path).items() if v is not None})
    return meta


def shoot_date(meta: dict, mtime: float) -> float:
    if meta.get("created"):
        return meta["created"]
    if meta.get("month") and meta.get("day"):
        year = datetime.fromtimestamp(mtime).year
        try:
            d = datetime(year, meta["month"], meta["day"], meta.get("hour", 12), meta.get("minute", 0))
            if d.timestamp() > mtime + 86400:  # clip from last year, copied in January
                d = d.replace(year=year - 1)
            return d.timestamp()
        except ValueError:
            pass
    return mtime


# ---- card offload ----------------------------------------------------------

def offload(src: Path, dest_root: Path, label: str, progress=lambda f, msg: None) -> dict:
    """Copy a camera card into the library with per-file checksum verification.

    Layout: <dest_root>/<YYYY-MM-DD>_<label>/<card folder>/...  plus a manifest.
    The copy is verified by re-reading the destination and comparing BLAKE2b hashes.
    Nothing on the source card is modified or deleted.
    """
    files = [p for p in src.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files) or 1
    day = datetime.now().strftime("%Y-%m-%d")
    safe_label = re.sub(r"[^\w\- ]+", "", label).strip() or "Shoot"
    dest = dest_root / f"{day}_{safe_label}" / src.name
    dest.mkdir(parents=True, exist_ok=True)
    done = 0
    manifest = []
    for p in files:
        rel = p.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        h_src = hashlib.blake2b(digest_size=32)
        tmp = out.with_name(out.name + ".partial")
        with p.open("rb") as fi, tmp.open("wb") as fo:
            while chunk := fi.read(8 * 1024 * 1024):
                h_src.update(chunk)
                fo.write(chunk)
                done += len(chunk)
                progress(done / total, f"Copying {rel}")
            fo.flush()
            os.fsync(fo.fileno())
        h_dst = hashlib.blake2b(digest_size=32)
        with tmp.open("rb") as fv:
            while chunk := fv.read(8 * 1024 * 1024):
                h_dst.update(chunk)
        if h_src.digest() != h_dst.digest():
            tmp.unlink(missing_ok=True)
            raise IOError(f"Checksum mismatch on {rel} — card or disk problem, offload stopped.")
        os.replace(tmp, out)
        os.utime(out, (p.stat().st_atime, p.stat().st_mtime))
        manifest.append({"path": str(rel).replace("\\", "/"), "size": p.stat().st_size, "blake2b": h_src.hexdigest()})
    (dest / "paddock-offload.json").write_text(json.dumps({
        "source": str(src), "label": label, "completed": time.time(), "files": manifest,
    }, indent=1))
    return {"dest": str(dest), "files": len(manifest), "bytes": done}
