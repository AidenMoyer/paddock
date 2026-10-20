"""Thumbnail and preview generation. Everything is cached under data/cache."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from . import db
from .config import settings
from .probe import run

THUMB_MAX = 640
PREVIEW_MAX = 2048


def thumb_path(item_id: int) -> Path:
    return settings.thumbs_dir / f"{item_id % 256:02x}" / f"{item_id}.webp"


def preview_path(item_id: int) -> Path:
    return settings.cache_dir / "previews" / f"{item_id % 256:02x}" / f"{item_id}.webp"


def _save(im: Image.Image, dest: Path, max_side: int, quality: int = 80) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    tmp = dest.with_suffix(".tmp")
    im.save(tmp, "WEBP", quality=quality, method=4)
    tmp.replace(dest)


def _open_image(path: Path) -> Image.Image | None:
    try:
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        im.load()
        return im
    except Exception:
        return _raw_embedded_preview(path)


def _raw_embedded_preview(path: Path) -> Image.Image | None:
    """Camera RAW files embed a full JPEG preview; ffmpeg can usually pull it out."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "p.jpg"
        try:
            run([settings.ffmpeg, "-v", "error", "-y", "-i", str(path), "-frames:v", "1", str(out)], timeout=60)
        except Exception:
            return None
        if out.exists():
            try:
                im = Image.open(out)
                im.load()
                return im
            except Exception:
                return None
    return None


def _video_frame(path: Path, duration: float | None) -> Image.Image | None:
    seek = 3.0
    if duration:
        seek = min(max(duration * 0.1, 0.0), max(duration - 0.5, 0.0))
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "f.png"
        # thumbnail filter picks the most representative frame of the next 50
        cmd = [settings.ffmpeg, "-v", "error", "-y", "-ss", f"{seek:.2f}", "-i", str(path),
               "-vf", "thumbnail=50,scale='min(1280,iw)':-2", "-frames:v", "1", str(out)]
        try:
            r = run(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        if r.returncode != 0 or not out.exists():
            try:
                run([settings.ffmpeg, "-v", "error", "-y", "-i", str(path), "-frames:v", "1", str(out)], timeout=120)
            except subprocess.TimeoutExpired:
                return None
        if out.exists():
            im = Image.open(out)
            im.load()
            return im
    return None


def _audio_cover(path: Path) -> Image.Image | None:
    try:
        import mutagen

        f = mutagen.File(path)
        if f is not None:
            pics = []
            if hasattr(f, "pictures"):
                pics = [p.data for p in f.pictures]
            if f.tags is not None:
                for k in list(f.tags.keys()):
                    if str(k).startswith("APIC"):
                        pics.append(f.tags[k].data)
                    elif k == "covr":
                        pics += [bytes(c) for c in f.tags[k]]
            for data in pics:
                try:
                    im = Image.open(io.BytesIO(data))
                    im.load()
                    return im
                except Exception:
                    continue
    except Exception:
        pass
    for name in ("cover.jpg", "folder.jpg", "front.jpg", "cover.png", "album.jpg", "Folder.jpg", "Cover.jpg"):
        p = path.parent / name
        if p.exists():
            return _open_image(p)
    return None


def _sidecar_poster(path: Path) -> Image.Image | None:
    """Local artwork next to the media (poster.jpg, folder.jpg, …) — no internet scraping."""
    for name in (f"{path.stem}-poster.jpg", f"{path.stem}.jpg", f"{path.stem}-thumb.jpg", "poster.jpg",
                 "folder.jpg", "cover.jpg"):
        p = path.parent / name
        if p.exists() and p != path:
            return _open_image(p)
    return None


def generate(item: dict) -> str:
    """Create the thumbnail for one item. Returns the new thumb_state."""
    path = Path(item["path"])
    kind = item["kind"]
    meta = json.loads(item.get("meta") or "{}")
    im: Image.Image | None = None
    try:
        if kind == "photo":
            im = _open_image(path)
        elif kind == "video":
            lib = db.q1("SELECT kind FROM libraries WHERE id=?", (item["library_id"],))
            if lib and lib["kind"] in ("movies", "shows"):
                im = _sidecar_poster(path)
            im = im or _video_frame(path, item.get("duration"))
        elif kind == "audio":
            im = _audio_cover(path)
        elif kind == "red_clip":
            if meta.get("proxy"):
                im = _video_frame(Path(meta["proxy"]), meta.get("duration"))
            if im is None:
                im = _red_thumbnail(path)
        elif kind == "braw_clip":
            im = _video_frame(path, item.get("duration"))
        elif kind in ("model3d", "gcode", "cad", "pcb"):
            from . import engineering

            _, embedded = engineering.describe(path)
            im = embedded
            if im is None and path.suffix.lower() in (".stl", ".obj", ".3mf"):
                from .stlrender import load_mesh, render

                if path.stat().st_size < 400 * 1024 * 1024:
                    im = render(load_mesh(path))
        elif kind == "document" and path.suffix.lower() == ".pdf":
            im = None
    except Exception:
        return "error"
    if im is None:
        return "none"
    _save(im, thumb_path(item["id"]), THUMB_MAX)
    return "ok"


def _red_thumbnail(path: Path) -> Image.Image | None:
    from .red import _redline_available

    if not _redline_available():
        return None
    with tempfile.TemporaryDirectory() as td:
        try:
            # format 3 = JPEG still in REDline; render a single frame 1/8 res
            run([settings.redline, "--i", str(path), "--outDir", td, "--o", "thumb", "--format", "3",
                 "--res", "8", "--start", "0", "--end", "0"], timeout=180)
        except Exception:
            return None
        files = sorted(Path(td).rglob("*.jpg"))
        if files:
            im = Image.open(files[0])
            im.load()
            return im
    return None


def preview(item: dict) -> Path | None:
    """Large web-friendly rendition of a photo (HEIC/RAW/TIFF → WebP)."""
    dest = preview_path(item["id"])
    if dest.exists() and dest.stat().st_mtime >= item["mtime"]:
        return dest
    im = _open_image(Path(item["path"]))
    if im is None:
        return None
    _save(im, dest, PREVIEW_MAX, quality=86)
    return dest
