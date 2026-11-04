"""File classification by extension."""

from __future__ import annotations

VIDEO = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".mts", ".m2ts", ".ts",
         ".3gp", ".mpg", ".mpeg", ".mxf", ".ogv", ".vob", ".insv", ".lrv"}
AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".alac",
         ".m4b", ".ape", ".wv", ".dsf", ".bwf"}
PHOTO = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".hif", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".avif",
         ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".srw", ".pef"}
RAW_PHOTO = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".srw", ".pef"}
RED = {".r3d"}
BRAW = {".braw"}
MODEL3D = {".stl", ".3mf", ".obj", ".ply", ".amf"}
GCODE = {".gcode", ".gco", ".g", ".bgcode"}
CAD = {".step", ".stp", ".iges", ".igs", ".sldprt", ".sldasm", ".slddrw", ".f3d", ".f3z", ".fcstd",
       ".dxf", ".dwg", ".scad", ".ipt", ".iam", ".idw", ".prt", ".asm", ".x_t", ".x_b", ".sat",
       ".3dm", ".skp", ".blend", ".catpart", ".catproduct", ".par", ".psm", ".jt"}
PCB = {".kicad_pcb", ".kicad_sch", ".kicad_pro", ".brd", ".sch", ".gbr", ".gtl", ".gbl", ".drl"}
DOCUMENT = {".pdf", ".md", ".txt"}
SUBTITLE = {".srt", ".vtt", ".ass", ".ssa", ".sub"}
SIDECAR = {".rmd", ".xmp", ".nfo", ".lrf", ".thm", ".aae"}

LIBRARY_KINDS = {
    "movies": "Movies",
    "shows": "TV Shows",
    "music": "Music",
    "photos": "Photos",
    "home_videos": "Home Videos",
    "red": "RED / Cinema Footage",
    "engineering": "Engineering (CAD & 3D Print)",
    "mixed": "Mixed",
}

# Which item kinds each library kind collects. The library type mainly decides how things are
# presented and grouped; it should never silently hide camera footage someone pointed it at.
_VISUAL = {"video", "photo", "red_clip", "braw_clip"}
LIBRARY_ACCEPTS = {
    "movies": {"video", "red_clip", "braw_clip"},   # not photos: poster.jpg artwork isn't an item
    "shows": {"video", "red_clip", "braw_clip"},
    "music": {"audio"},
    "photos": _VISUAL,
    "home_videos": _VISUAL | {"audio"},
    "red": _VISUAL | {"audio"},
    "engineering": {"model3d", "gcode", "cad", "pcb", "document", "photo", "video"},
    "mixed": _VISUAL | {"audio", "model3d", "gcode", "cad", "pcb", "document"},
}


def classify(ext: str) -> str | None:
    ext = ext.lower()
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext in PHOTO:
        return "photo"
    if ext in RED:
        return "red_clip"
    if ext in BRAW:
        return "braw_clip"
    if ext in MODEL3D:
        return "model3d"
    if ext in GCODE:
        return "gcode"
    if ext in CAD:
        return "cad"
    if ext in PCB:
        return "pcb"
    if ext in DOCUMENT:
        return "document"
    return None


# Codecs a modern phone/browser decodes without help. Anything else is transcoded.
BROWSER_VIDEO_CODECS = {"h264", "vp9", "av1", "vp8"}
BROWSER_AUDIO_CODECS = {"aac", "mp3", "opus", "vorbis", "flac"}
BROWSER_CONTAINERS = {".mp4", ".m4v", ".webm", ".mov"}

MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".ts": "video/mp2t", ".mts": "video/mp2t",
    ".m2ts": "video/mp2t", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".gif": "image/gif", ".heic": "image/heic", ".heif": "image/heif", ".hif": "image/heif", ".avif": "image/avif",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp",
    ".pdf": "application/pdf", ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".stl": "model/stl", ".3mf": "model/3mf", ".obj": "model/obj", ".step": "model/step", ".stp": "model/step",
    ".gcode": "text/x-gcode", ".vtt": "text/vtt", ".srt": "application/x-subrip",
}


def mime_for(ext: str) -> str:
    return MIME.get(ext.lower(), "application/octet-stream")


# The extension to give a file when all we know is its type. Only what phones and browsers report for media.
EXT_FOR_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/heic": ".heic", "image/heic-sequence": ".heic", "image/heif": ".heif", "image/heif-sequence": ".heif",
    "image/avif": ".avif", "image/bmp": ".bmp", "image/tiff": ".tif", "image/x-adobe-dng": ".dng", "image/dng": ".dng",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/3gpp": ".3gp", "video/mp2t": ".ts", "video/x-msvideo": ".avi",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/flac": ".flac", "audio/ogg": ".ogg",
    "audio/opus": ".opus", "audio/wav": ".wav", "audio/x-wav": ".wav",
}


def ext_for_mime(mime: str | None) -> str | None:
    return EXT_FOR_MIME.get((mime or "").split(";")[0].strip().lower())
