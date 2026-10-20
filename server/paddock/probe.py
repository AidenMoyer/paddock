"""Metadata extraction: ffprobe for A/V, EXIF for photos, tags for music, names for movies/TV."""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from .config import settings

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

from PIL import ExifTags, Image


def run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


# ---- ffprobe ---------------------------------------------------------------

def ffprobe(path: Path) -> dict | None:
    try:
        r = run([settings.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def parse_iso6709(s: str) -> tuple[float, float] | None:
    m = _ISO6709.match(s.strip())
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
        return lat, lon
    return None


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            d = dt.datetime.fromisoformat(s) if fmt is None else dt.datetime.strptime(s, fmt)
            if d.year < 1971:
                return None
            if d.tzinfo is None:
                d = d.astimezone()  # camera clocks are local time
            return d.timestamp()
        except ValueError:
            continue
    return None


def av_info(path: Path) -> dict:
    """Normalized A/V facts from ffprobe."""
    info = ffprobe(path)
    if not info:
        return {}
    fmt = info.get("format", {})
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    out: dict = {"container": fmt.get("format_name"), "bitrate": _int(fmt.get("bit_rate"))}
    dur = _float(fmt.get("duration"))
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    subs = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    if v:
        out["vcodec"] = v.get("codec_name")
        out["width"] = v.get("width")
        out["height"] = v.get("height")
        out["pix_fmt"] = v.get("pix_fmt")
        out["fps"] = _rate(v.get("avg_frame_rate") or v.get("r_frame_rate"))
        out["hdr"] = v.get("color_transfer") in ("smpte2084", "arib-std-b67")
        rot = None
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = sd["rotation"]
        if rot is None and (v.get("tags") or {}).get("rotate"):
            rot = _int(v["tags"]["rotate"])
        if rot and abs(int(rot)) % 180 == 90:
            out["width"], out["height"] = out["height"], out["width"]
        dur = dur or _float(v.get("duration"))
    if a:
        out["acodec"] = a.get("codec_name")
        out["channels"] = a.get("channels")
        out["sample_rate"] = _int(a.get("sample_rate"))
        dur = dur or _float(a.get("duration"))
    if subs:
        out["subtitle_streams"] = [
            {"index": s["index"], "codec": s.get("codec_name"), "lang": (s.get("tags") or {}).get("language")}
            for s in subs
        ]
    out["audio_streams"] = len([s for s in info.get("streams", []) if s.get("codec_type") == "audio"])
    out["duration"] = dur
    out["created"] = parse_time(tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate"))
    loc = tags.get("location") or tags.get("com.apple.quicktime.location.iso6709") or tags.get("location-eng")
    if loc:
        ll = parse_iso6709(loc)
        if ll:
            out["lat"], out["lon"] = ll
    for k in ("make", "model", "com.apple.quicktime.make", "com.apple.quicktime.model", "encoder", "title",
              "artist", "album", "comment"):
        if tags.get(k):
            out[k.split(".")[-1]] = tags[k]
    return out


def _float(v) -> float | None:
    try:
        f = float(v)
        return f if f == f and f > 0 else None
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rate(r: str | None) -> float | None:
    if not r or "/" not in r:
        return _float(r)
    n, d = r.split("/", 1)
    try:
        return round(float(n) / float(d), 3) if float(d) else None
    except ValueError:
        return None


# ---- photos ----------------------------------------------------------------

_GPS_TAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}


def _dms(v, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in v)
        val = d + m / 60 + s / 3600
        return -val if ref in ("S", "W") else val
    except Exception:
        return None


def photo_info(path: Path) -> dict:
    out: dict = {}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            exif = im.getexif()
            if not exif:
                return out
            base = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            sub = {ExifTags.TAGS.get(k, k): v for k, v in exif.get_ifd(0x8769).items()}
            gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in exif.get_ifd(0x8825).items()}
            orient = base.get("Orientation")
            if orient in (5, 6, 7, 8):
                out["width"], out["height"] = out["height"], out["width"]
            taken = sub.get("DateTimeOriginal") or sub.get("DateTimeDigitized") or base.get("DateTime")
            if isinstance(taken, str):
                offset = sub.get("OffsetTimeOriginal")
                out["created"] = parse_time(taken + (offset or "")) or parse_time(taken)
            for k in ("Make", "Model", "Software"):
                if isinstance(base.get(k), str):
                    out[k.lower()] = base[k].strip("\x00 ")
            for k, name in (("LensModel", "lens"), ("FNumber", "f_number"), ("ExposureTime", "exposure"),
                            ("ISOSpeedRatings", "iso"), ("FocalLength", "focal_length")):
                v = sub.get(k)
                if v is not None:
                    try:
                        out[name] = float(v) if not isinstance(v, str) else v.strip("\x00 ")
                    except (TypeError, ValueError):
                        pass
            if gps.get("GPSLatitude") and gps.get("GPSLongitude"):
                lat = _dms(gps["GPSLatitude"], gps.get("GPSLatitudeRef"))
                lon = _dms(gps["GPSLongitude"], gps.get("GPSLongitudeRef"))
                if lat is not None and lon is not None and not (lat == 0 and lon == 0):
                    out["lat"], out["lon"] = round(lat, 6), round(lon, 6)
    except Exception:
        pass
    return out


# ---- music -----------------------------------------------------------------

def audio_tags(path: Path) -> dict:
    out: dict = {}
    try:
        import mutagen

        f = mutagen.File(path, easy=True)
        if f is None:
            return out
        if getattr(f, "info", None) and getattr(f.info, "length", None):
            out["duration"] = float(f.info.length)
        tags = f.tags or {}
        for key in ("title", "artist", "album", "albumartist", "genre", "date", "tracknumber", "discnumber"):
            v = tags.get(key)
            if v:
                out[key] = str(v[0]) if isinstance(v, list) else str(v)
        if "tracknumber" in out:
            out["track"] = _int(out.pop("tracknumber").split("/")[0])
        if "discnumber" in out:
            out["disc"] = _int(out.pop("discnumber").split("/")[0])
    except Exception:
        pass
    return out


# ---- names -----------------------------------------------------------------

_JUNK = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr10?|dv|bluray|blu-ray|brrip|bdrip|webrip|web-dl|webdl|hdtv|dvdrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|ac3|dts|atmos|truehd|remux|proper|repack|extended|10bit|"
    r"yts|rarbg|amzn|nf|ddp?5\.1)\b.*$",
    re.I,
)
_YEAR = re.compile(r"[\(\[\s\.]((?:19|20)\d\d)[\)\]\s\.]")
_EP = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})|(\d{1,2})x(\d{2,3})")


def clean_title(stem: str) -> str:
    t = stem.replace(".", " ").replace("_", " ")
    t = _JUNK.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -([")
    return t or stem


def parse_movie(path: Path) -> dict:
    stem = path.stem
    m = _YEAR.search(f" {stem} ")
    year = int(m.group(1)) if m else None
    title = stem[: m.start() - 1] if m and m.start() > 1 else stem  # m indexes the space-padded stem
    title = clean_title(title)
    if year is None:  # "Movie Name (2010)/file.mkv"
        pm = _YEAR.search(f" {path.parent.name} ")
        if pm:
            year = int(pm.group(1))
            title = clean_title(path.parent.name[: pm.start()]) or title
    return {"title": title, "year": year}


def parse_episode(path: Path, library_root: Path) -> dict:
    stem = path.stem
    m = _EP.search(stem)
    out: dict = {}
    if m:
        season = int(m.group(1) or m.group(3))
        episode = int(m.group(2) or m.group(4))
        out["season"], out["episode"] = season, episode
        before = clean_title(stem[: m.start()])
        after = clean_title(stem[m.end():])
        out["episode_title"] = after or None
        show = before
    else:
        show = ""
    try:
        parts = path.relative_to(library_root).parts
    except ValueError:
        parts = ()
    if len(parts) >= 2:  # Show/Season 1/file  or  Show/file
        show = clean_title(parts[0])
        if "season" not in out:
            sm = re.search(r"season\s*(\d+)", " ".join(parts[1:-1]), re.I)
            if sm:
                out["season"] = int(sm.group(1))
    out["show"] = show or clean_title(path.parent.name)
    return out
