"""Playback: quality levels, hardware transcoding, adaptive streaming, caching.

Quality the viewer can pick at any time (originals are never modified):
  * Auto      — adaptive HLS master playlist; the player climbs to the best rung the
                network sustains (including Original, when the device can decode it).
  * Original  — the untouched video bitstream. Direct file when the client can play
                the container, otherwise a stream-copy HLS remux on keyframe
                boundaries (video is bit-identical; only audio is converted to AAC).
  * 2160p … 360p — transcoded H.264 rungs, only those ≤ the source resolution.

Transcoding uses the GPU when one is present (NVIDIA NVENC/CUDA, Intel QuickSync,
VAAPI for AMD/Intel): decode, scale and encode stay on the GPU; HDR sources are
tone-mapped to SDR; any GPU failure falls back to the CPU automatically.

Caching: transcoded segments are kept on disk (LRU, size-capped) so seeking back,
re-watching and other viewers are instant; encoders run a couple of minutes ahead
of the viewer and are paused when far enough ahead; "Optimize" pre-builds rungs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, kinds
from .config import settings

log = logging.getLogger("paddock.transcode")

SEGMENT = 6.0

# name: (height, video kbps, audio kbps)
PROFILES: dict[str, tuple[int, int, int]] = {
    "2160p": (2160, 20000, 192),
    "1440p": (1440, 12000, 192),
    "1080p": (1080, 8000, 192),
    "720p": (720, 4000, 160),
    "480p": (480, 1500, 128),
    "360p": (360, 800, 96),
}
GPU_DECODABLE = {"h264", "hevc", "vp9", "av1", "mpeg2video", "vc1", "vp8", "mjpeg"}


def transcode_settings() -> dict:
    cfg = {"hwaccel": settings.hwaccel or "auto", "cache_gb": 50, "ahead_segments": 20, "encoder": "h264"}
    cfg.update(db.get_setting("transcode", {}))
    return cfg


# ---- hardware detection -------------------------------------------------------------

_hw_cache: dict | None = None
_hw_lock = threading.Lock()


def _ffmpeg_has(what: str, name: str) -> bool:
    try:
        out = subprocess.run([settings.ffmpeg, "-hide_banner", f"-{what}"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f" {name} " in out


WINDOWS = os.name == "nt"


def _run_test(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, ("" if r.returncode == 0 else _explain(r.stderr.decode(errors="ignore")))


_GENERIC = ("error while", "terminating thread", "nothing was written", "could not open encoder", "error initializing",
            "conversion failed", "exiting with")


def _explain(err: str) -> str:
    """Turn ffmpeg's hardware errors into something an admin can act on."""
    import re

    low = err.lower()
    if "nvenc api version" in low or "minimum required nvidia driver" in low:
        need = re.search(r"(\d{3}\.\d+) or newer", err)
        return (f"NVIDIA driver too old for this ffmpeg's NVENC encoder (needs driver {need.group(1) if need else 'a newer one'}). "
                "GTX 10-series and older cards can't install that driver; Paddock still uses NVIDIA for decoding when it can.")
    if "nvml" in low or ("cuda" in low and "fail" in low):
        return "NVIDIA GPU not available to ffmpeg (driver not loaded, or GPU not given to the app)."
    if "amfrt" in low:
        return "AMD AMF runtime not installed."
    if "renderd128" in low or "vaapi" in low:
        return "No VAAPI render device (/dev/dri) available."
    # otherwise the first specific line (the root cause), without ffmpeg's "[h264_qsv @ 0x...]" prefixes
    lines = [re.sub(r"\[[^\]]+ @ [0-9a-fx]+\]\s*", "", ln).strip() for ln in err.splitlines()]
    lines = [ln for ln in lines if ln and not any(g in ln.lower() for g in _GENERIC)]
    return (lines[0] if lines else err.strip())[:240]


def _test_encode(method: str) -> tuple[bool, str]:
    if WINDOWS and method in ("qsv", "nvenc"):
        # Windows: the encoder takes system-memory frames directly (no VAAPI/CUDA device plumbing)
        return _run_test([settings.ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=1",
                          "-vf", "scale=640:360,format=nv12", "-c:v", {"qsv": "h264_qsv", "nvenc": "h264_nvenc"}[method], "-f", "null", "-"])
    args = _hw_input_args(method, for_test=True) + ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=1"]
    vf = {"nvenc": "format=nv12,hwupload_cuda,scale_cuda=640:360",
          "qsv": "format=nv12,hwupload=extra_hw_frames=16,vpp_qsv=w=640:h=360",
          "vaapi": "format=nv12,hwupload,scale_vaapi=w=640:h=360"}[method]
    enc = {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "vaapi": "h264_vaapi"}[method]
    return _run_test([settings.ffmpeg, "-v", "error", "-nostdin", *args, "-vf", vf, "-c:v", enc, "-f", "null", "-"])


def _test_cuda_decode() -> tuple[bool, str]:
    """NVIDIA decode + scale works even where NVENC doesn't (e.g. drivers too old for this ffmpeg's encoder)."""
    return _run_test([settings.ffmpeg, "-v", "error", "-nostdin", "-init_hw_device", "cuda=cu:0", "-filter_hw_device", "cu",
                      "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=0.3",
                      "-vf", "format=nv12,hwupload_cuda,scale_cuda=320:180:format=nv12,hwdownload,format=nv12", "-f", "null", "-"])


def _gpu_names() -> list[str]:
    if not WINDOWS:
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.Status }"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def detect_hardware(force: bool = False) -> dict:
    """Which GPU encoders actually work in this container (verified with a test encode)."""
    global _hw_cache
    with _hw_lock:
        if _hw_cache is not None and not force:
            return _hw_cache
        found: dict = {"devices": [], "working": [], "checked_at": time.time()}
        render = Path("/dev/dri/renderD128")
        if render.exists():
            vendor = ""
            try:
                vendor = Path("/sys/class/drm/renderD128/device/vendor").read_text().strip()
            except OSError:
                pass
            found["devices"].append({"path": str(render), "vendor": {"0x8086": "Intel", "0x1002": "AMD",
                                                                     "0x10de": "NVIDIA"}.get(vendor, vendor or "unknown")})
        names = _gpu_names()
        if names:
            for n in names:
                name, _, status = n.partition("|")
                if any(v in name for v in ("NVIDIA", "Intel", "AMD", "Radeon")):
                    found["devices"].append({"path": name, "vendor": name.split()[0], "status": status})
        elif Path("/dev/nvidia0").exists() or shutil.which("nvidia-smi"):
            found["devices"].append({"path": "/dev/nvidia0", "vendor": "NVIDIA"})
        found["errors"] = {}
        for method, enc in (("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"), ("vaapi", "h264_vaapi")):
            if (WINDOWS and method == "vaapi") or not _ffmpeg_has("encoders", enc):
                continue
            ok, why = _test_encode(method)
            if ok:
                found["working"].append(method)
            else:
                found["errors"][method] = why
        found["decode"] = []
        if "nvenc" in found["working"]:
            found["decode"].append("cuda")
        else:
            ok, why = _test_cuda_decode()
            if ok:
                found["decode"].append("cuda")
            elif any(d.get("vendor") == "NVIDIA" for d in found["devices"]):
                found["errors"]["cuda"] = why
        _hw_cache = found
        return found


def cuda_decode_available() -> bool:
    return transcode_settings()["hwaccel"] != "none" and "cuda" in detect_hardware().get("decode", [])


def pipeline_label(short: bool = False) -> str:
    """Human description of what transcoding uses right now."""
    enc = active_hwaccel()
    names = ({"nvenc": "NVENC", "qsv": "QSV", "vaapi": "VAAPI", "none": "CPU"} if short else
             {"nvenc": "NVIDIA NVENC", "qsv": "Intel Quick Sync", "vaapi": "VAAPI", "none": "CPU"})
    if enc == "nvenc":
        return "NVENC" if short else "NVIDIA (decode + encode)"
    if cuda_decode_available() and (WINDOWS or enc == "none"):
        return f"NVDEC + {names[enc]}" if short else f"NVIDIA decode + {names[enc]} encode"
    return names[enc]


def active_hwaccel() -> str:
    want = transcode_settings()["hwaccel"]
    if want == "none":
        return "none"
    working = detect_hardware()["working"]
    if want == "auto":
        return working[0] if working else "none"
    return want if want in working else "none"


def _hw_input_args(method: str, for_test: bool = False) -> list[str]:
    if method == "nvenc":
        return ["-init_hw_device", "cuda=cu:0", "-filter_hw_device", "cu"] if for_test else \
            ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if method == "qsv":
        base = ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs"]
        return base if for_test else base + ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    if method == "vaapi":
        base = ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va"]
        return base if for_test else base + ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
    return []


# ---- playback decisions --------------------------------------------------------------------

def available_profiles(meta: dict) -> list[str]:
    src_h = meta.get("height") or 1080
    src_w = meta.get("width") or 1920
    short_side = min(src_h, src_w) if src_w and src_h else src_h
    out = [p for p, (h, _, _) in PROFILES.items() if h <= short_side * 1.05]
    return out or ["360p"]


def can_direct(item: dict, meta: dict, client: set[str]) -> bool:
    if item["kind"] == "audio":
        return (meta.get("acodec") or item["ext"].lstrip(".")) in client or item["ext"] in (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav")
    vc, ac = meta.get("vcodec"), meta.get("acodec")
    container_ok = item["ext"] in kinds.BROWSER_CONTAINERS or ("mkv" in client and item["ext"] in (".mkv", ".webm")) \
        or ("ts" in client and item["ext"] in (".ts", ".m2ts", ".mts"))
    return container_ok and vc in client and (ac is None or ac in client)


def can_copy(meta: dict, client: set[str]) -> bool:
    """Video bitstream can be remuxed untouched for this client."""
    return meta.get("vcodec") in client and meta.get("vcodec") in ("h264", "hevc", "av1") and not meta.get("hdr_needs_tonemap")


def playback_plan(item: dict, meta: dict, client_codecs: set[str] | None = None) -> dict:
    client = client_codecs or (kinds.BROWSER_VIDEO_CODECS | kinds.BROWSER_AUDIO_CODECS)
    if item["kind"] == "red_clip":
        if not meta.get("proxy"):
            return {"mode": "unavailable", "reason": "RED clip has no proxy yet. Install REDline or add camera proxies."}
        return {"mode": "transcode", "source": "proxy", "profiles": available_profiles(meta), "original": False,
                "reason": "R3D plays from its proxy"}
    if item["kind"] == "braw_clip":
        return {"mode": "unavailable", "reason": "Blackmagic RAW needs a proxy (ProRes/H.264) next to the clip."}
    direct = can_direct(item, meta, client)
    plan = {"mode": "direct" if direct else "transcode", "profiles": available_profiles(meta) if item["kind"] == "video" else [],
            "original": direct or (item["kind"] == "video" and can_copy(meta, client))}
    if not direct:
        why = []
        if meta.get("vcodec") not in client and item["kind"] == "video":
            why.append(f"video codec {meta.get('vcodec')}")
        if meta.get("acodec") and meta.get("acodec") not in client:
            why.append(f"audio codec {meta.get('acodec')}")
        if item["ext"] not in kinds.BROWSER_CONTAINERS:
            why.append(f"container {item['ext']}")
        plan["reason"] = "needs conversion: " + ", ".join(why) if why else "needs conversion"
    return plan


# ---- ffmpeg argument building ---------------------------------------------------------------

def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _target_size(meta: dict, height: int) -> tuple[int, int]:
    sw, sh = meta.get("width") or 1920, meta.get("height") or 1080
    if sw >= sh:
        h = min(height, sh)
        return _even(sw * h / sh), _even(h)
    w = min(height, sw)  # portrait phone video: limit the short side
    return _even(w), _even(sh * w / sw)


def _tonemap(meta: dict) -> str:
    return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


def build_video_args(meta: dict, profile: str, method: str, cuda_decode: bool = False) -> tuple[list[str], list[str]]:
    """(input args, output video args) for one rung on the chosen hardware."""
    height, vk, _ = PROFILES[profile]
    w, h = _target_size(meta, height)
    rate = ["-b:v", f"{vk}k", "-maxrate", f"{int(vk * 1.4)}k", "-bufsize", f"{vk * 2}k"]
    gop = ["-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT})"]
    hdr = bool(meta.get("hdr"))
    gpu_decode = meta.get("vcodec") in GPU_DECODABLE and not hdr and meta.get("pix_fmt", "").find("422") < 0
    pf = meta.get("pix_fmt") or ""
    nvdec_ok = (meta.get("vcodec") in {"h264", "hevc", "vp9", "mpeg2video", "vc1", "mjpeg"} and not any(x in pf for x in ("422", "444", "12"))
                and not (meta.get("vcodec") == "h264" and "10" in pf))   # NVDEC has no 4:2:2/4:4:4, 12-bit or 10-bit H.264

    # Hybrid: NVIDIA decodes and scales (the heavy part for 4K/6K HEVC), then the frames go to the
    # Intel or CPU encoder. Used when NVENC itself isn't usable (e.g. GTX 10-series with older drivers).
    if cuda_decode and method in ("qsv", "none") and nvdec_ok and (WINDOWS or method == "none"):
        if hdr:
            vf = f"scale_cuda={w}:{h},hwdownload,format=p010le," + _tonemap(meta)
        else:
            vf = f"scale_cuda={w}:{h}:format=nv12,hwdownload,format=nv12"
        if method == "qsv":
            enc = ["-c:v", "h264_qsv", "-preset", "veryfast", *rate, "-profile:v", "high", *gop]
        else:
            enc = ["-c:v", "libx264", "-preset", "superfast", "-profile:v", "high", "-crf", "22",
                   "-maxrate", f"{vk}k", "-bufsize", f"{vk * 2}k", *gop]
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"], ["-vf", vf, *enc]

    if WINDOWS and method in ("qsv", "nvenc"):
        # GPU-assisted decode (D3D11) with frames copied back, CPU scale, GPU encode
        enc = ["-c:v", "h264_qsv", "-preset", "veryfast"] if method == "qsv" else ["-c:v", "h264_nvenc", "-preset", "p3"]
        vf = (_tonemap(meta) + "," if hdr else "") + f"scale={w}:{h}:flags=fast_bilinear,format=nv12"
        return ["-hwaccel", "auto"], ["-vf", vf, *enc, *rate, "-profile:v", "high", *gop]
    if method == "nvenc":
        enc = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr", *rate, "-profile:v", "high", *gop]
        if gpu_decode:
            return _hw_input_args("nvenc"), ["-vf", f"scale_cuda={w}:{h}:format=yuv420p", *enc]
        vf = (_tonemap(meta) + "," if hdr else "") + f"scale={w}:{h},format=yuv420p"
        return [], ["-vf", vf, *enc]
    if method == "qsv":
        enc = ["-c:v", "h264_qsv", "-preset", "medium", *rate, "-profile:v", "high", *gop]
        if gpu_decode:
            return _hw_input_args("qsv"), ["-vf", f"vpp_qsv=w={w}:h={h}:format=nv12", *enc]
        vf = (_tonemap(meta) + "," if hdr else "") + f"scale={w}:{h},format=nv12"
        return [], ["-vf", vf, *enc]
    if method == "vaapi":
        enc = ["-c:v", "h264_vaapi", *rate, "-profile:v", "high", *gop]
        if gpu_decode:
            return _hw_input_args("vaapi"), ["-vf", f"scale_vaapi=w={w}:h={h}:format=nv12", *enc]
        vf = (_tonemap(meta) + "," if hdr else "") + f"scale={w}:{h},format=nv12,hwupload"
        return ["-init_hw_device", "vaapi=va:/dev/dri/renderD128", "-filter_hw_device", "va"], ["-vf", vf, *enc]
    vf = (_tonemap(meta) + "," if hdr else "") + f"scale={w}:{h}:flags=bicubic,format=yuv420p"
    return [], ["-vf", vf, "-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency", "-profile:v", "high", "-crf", "22",
                "-maxrate", f"{vk}k", "-bufsize", f"{vk * 2}k", *gop]


def _codec_string(meta: dict) -> str | None:
    vc = meta.get("vcodec")
    if vc == "h264":
        return "avc1.640028"
    if vc == "hevc":
        return "hvc1.2.4.L153.B0" if "10" in (meta.get("pix_fmt") or "") else "hvc1.1.6.L150.B0"
    if vc == "av1":
        return "av01.0.08M.10" if "10" in (meta.get("pix_fmt") or "") else "av01.0.08M.08"
    return None


# ---- HLS playlists ------------------------------------------------------------------------------

def master_playlist(meta: dict, include_original: bool) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    fps = meta.get("fps")
    if include_original and meta.get("bitrate") and _codec_string(meta):
        bw = int(meta["bitrate"] * 1.1)
        lines += [f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={meta.get("width")}x{meta.get("height")},'
                  f'CODECS="{_codec_string(meta)},mp4a.40.2"' + (f",FRAME-RATE={fps:.3f}" if fps else ""), "original/index.m3u8"]
    for p in available_profiles(meta):
        height, vk, ak = PROFILES[p]
        w, h = _target_size(meta, height)
        bw = int((vk + ak) * 1000 * 1.15)
        lines += [f'#EXT-X-STREAM-INF:BANDWIDTH={bw},AVERAGE-BANDWIDTH={int((vk + ak) * 1000)},RESOLUTION={w}x{h},'
                  f'CODECS="avc1.640028,mp4a.40.2"' + (f",FRAME-RATE={min(fps, 60):.3f}" if fps else ""),
                  f"{p}/index.m3u8"]
    return "\n".join(lines) + "\n"


def media_playlist(durations: list[float]) -> str:
    target = math.ceil(max(durations) if durations else SEGMENT)
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for i, d in enumerate(durations):
        lines += [f"#EXTINF:{max(d, 0.05):.3f},", f"seg_{i}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def fixed_segments(duration: float) -> list[float]:
    n = max(1, math.ceil(duration / SEGMENT))
    return [min(SEGMENT, duration - i * SEGMENT) for i in range(n)]


def keyframe_segments(source: Path, item_id: int, mtime: float) -> list[tuple[float, float]]:
    """Segment boundaries on the source's own keyframes (for stream-copy 'Original'). Cached."""
    cache = settings.cache_dir / "keyframes" / f"{item_id}-{int(mtime)}.json"
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text())]
    r = subprocess.run([settings.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "packet=pts_time,flags", "-of", "csv=p=0", str(source)], capture_output=True, text=True,
                       timeout=600)
    kfs, last = [], 0.0
    for line in r.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                kfs.append(float(parts[0]))
            except ValueError:
                pass
        if parts and parts[0]:
            try:
                last = max(last, float(parts[0]))
            except ValueError:
                pass
    kfs = sorted(set(kfs)) or [0.0]
    segs: list[tuple[float, float]] = []
    start = kfs[0]
    for k in kfs[1:]:
        if k - start >= SEGMENT:
            segs.append((start, k - start))
            start = k
    segs.append((start, max(last - start, 0.1)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(segs))
    return segs


# ---- segment production & cache --------------------------------------------------------------------

@dataclass
class EncodeJob:
    key: str
    dir: Path
    proc: subprocess.Popen | None = None
    start_seg: int = 0
    last_request_seg: int = 0
    last_access: float = field(default_factory=time.time)
    paused: bool = False
    method: str = "none"
    started_at: float = 0.0


def cache_dir_for(item: dict, profile: str, source: Path) -> Path:
    tag = hashlib.sha1(f"{source}:{int(item['mtime'])}".encode()).hexdigest()[:10]
    return settings.transcode_dir / f"{item['id']}-{tag}" / profile


class SegmentManager:
    IDLE_KILL_S = 60
    LOOKAHEAD_RESTART = 4    # requested segment this far beyond the encoder → restart there

    def __init__(self):
        self.jobs: dict[str, EncodeJob] = {}
        self.copy_locks: dict[str, threading.Lock] = {}
        self.lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            threading.Thread(target=self._housekeeping, daemon=True, name="transcode-housekeeping").start()

    # -- public ------------------------------------------------------------------------------
    def segment(self, item: dict, meta: dict, source: Path, profile: str, seg: int, timeout: float = 60) -> Path | None:
        self.start()
        d = cache_dir_for(item, profile, source)
        target = d / f"seg_{seg}.ts"
        if target.exists():
            _touch(d.parent)
            self._note_request(d, seg)
            return target
        if profile == "original":
            return self._copy_segment(item, source, d, seg)
        key = str(d)
        with self.lock:
            job = self.jobs.get(key)
            if job is None:
                job = EncodeJob(key=key, dir=d)
                self.jobs[key] = job
            job.last_access = time.time()
            job.last_request_seg = seg
            running = job.proc is not None and job.proc.poll() is None
            pos = self._encoder_position(job)
            if not running or seg < job.start_seg or seg > pos + self.LOOKAHEAD_RESTART:
                self._start_encoder(job, item, meta, source, profile, seg)
            elif job.paused:
                self._resume(job)
        deadline = time.time() + timeout
        retried = False
        while time.time() < deadline:
            if target.exists():
                _touch(d.parent)
                return target
            proc = job.proc
            if proc is not None and proc.poll() is not None and not target.exists():
                if proc.returncode != 0 and job.method != "none" and not retried:
                    log.warning("GPU transcode (%s) failed for item %s — retrying on CPU", job.method, item["id"])
                    retried = True
                    with self.lock:
                        self._start_encoder(job, item, meta, source, profile, seg, force_cpu=True)
                    continue
                time.sleep(0.3)
                return target if target.exists() else None
            time.sleep(0.08)
        return None

    def stop_all(self) -> None:
        with self.lock:
            for job in self.jobs.values():
                _kill(job)

    def status(self) -> list[dict]:
        out = []
        with self.lock:
            for j in self.jobs.values():
                if j.proc and j.proc.poll() is None:
                    out.append({"dir": str(j.dir.relative_to(settings.transcode_dir)), "method": j.method,
                                "paused": j.paused, "at_segment": self._encoder_position(j),
                                "viewer_segment": j.last_request_seg, "running_s": round(time.time() - j.started_at)})
        return out

    # -- internals ----------------------------------------------------------------------------
    def _note_request(self, d: Path, seg: int) -> None:
        with self.lock:
            job = self.jobs.get(str(d))
            if job:
                job.last_access = time.time()
                job.last_request_seg = seg
                if job.paused and self._encoder_position(job) - seg < transcode_settings()["ahead_segments"] // 2:
                    self._resume(job)

    def _encoder_position(self, job: EncodeJob) -> int:
        best = job.start_seg - 1
        n = job.start_seg
        while (job.dir / f"seg_{n}.ts").exists():
            best = n
            n += 1
        return best

    def _start_encoder(self, job: EncodeJob, item: dict, meta: dict, source: Path, profile: str, seg: int,
                       force_cpu: bool = False) -> None:
        _kill(job)
        job.dir.mkdir(parents=True, exist_ok=True)
        cpu_only = force_cpu or meta.get("gpu_failed")
        method = "none" if cpu_only else active_hwaccel()
        cuda = (not cpu_only) and cuda_decode_available()
        in_args, v_args = build_video_args(meta, profile, method, cuda_decode=cuda)
        _, _, ak = PROFILES[profile]
        # resume after already-cached segments instead of re-encoding them
        while (job.dir / f"seg_{seg}.ts").exists():
            seg += 1
        start = seg * SEGMENT
        cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-y", *in_args, "-ss", f"{start:.3f}", "-i", str(source),
               "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn", *v_args,
               "-c:a", "aac", "-ac", "2", "-b:a", f"{ak}k",
               "-f", "hls", "-hls_time", f"{SEGMENT}", "-hls_playlist_type", "event",
               "-hls_flags", "temp_file+independent_segments", "-start_number", str(seg),
               "-hls_segment_filename", str(job.dir / "seg_%d.ts"),
               "-output_ts_offset", f"{start:.3f}", "-muxdelay", "0", str(job.dir / f"ffmpeg-{seg}.m3u8")]
        log.info("encode item=%s profile=%s seg=%s hw=%s", item["id"], profile, seg, method)
        with (job.dir / "ffmpeg.log").open("ab") as errlog:
            job.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errlog)
        job.start_seg = seg
        job.method = ("cuda+" if cuda and method != "nvenc" else "") + method
        job.paused = False
        job.started_at = time.time()
        if force_cpu and not meta.get("gpu_failed"):
            meta["gpu_failed"] = True  # remember: this file needs the CPU path
            row = db.q1("SELECT meta FROM items WHERE id=?", (item["id"],))
            if row:
                stored = json.loads(row["meta"] or "{}")
                stored["gpu_failed"] = True
                db.x("UPDATE items SET meta=? WHERE id=?", (json.dumps(stored, default=str), item["id"]))

    def _copy_segment(self, item: dict, source: Path, d: Path, seg: int) -> Path | None:
        segs = keyframe_segments(source, item["id"], item["mtime"])
        if seg >= len(segs):
            return None
        start, dur = segs[seg]
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"seg_{seg}.ts"
        with self.lock:
            seg_lock = self.copy_locks.setdefault(str(target), threading.Lock())
        with seg_lock:  # a prefetch and a player request may want the same segment at once
            if target.exists():
                return target
            return self._copy_segment_locked(item, source, d, seg, segs, start, dur, target)

    def _copy_segment_locked(self, item: dict, source: Path, d: Path, seg: int, segs: list, start: float, dur: float,
                             target: Path) -> Path | None:
        tmp = d / f"seg_{seg}.{threading.get_ident()}.tmp.ts"
        cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", f"{start:.6f}", "-i", str(source),
               "-t", f"{dur:.6f}", "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-c:v", "copy",
               "-c:a", "aac", "-ac", "2", "-b:a", "256k", "-f", "mpegts", "-output_ts_offset", f"{start:.6f}",
               "-muxdelay", "0", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not tmp.exists():
            return None
        tmp.replace(target)
        _touch(d.parent)
        # warm the next couple of segments in the background for smooth playback
        for nxt in (seg + 1, seg + 2):
            if nxt < len(segs) and not (d / f"seg_{nxt}.ts").exists():
                threading.Thread(target=self._copy_segment, args=(item, source, d, nxt), daemon=True).start()
                break
        return target

    def _pause(self, job: EncodeJob) -> None:
        if hasattr(signal, "SIGSTOP") and job.proc and job.proc.poll() is None and not job.paused:
            os.kill(job.proc.pid, signal.SIGSTOP)
            job.paused = True

    def _resume(self, job: EncodeJob) -> None:
        if hasattr(signal, "SIGCONT") and job.proc and job.proc.poll() is None and job.paused:
            os.kill(job.proc.pid, signal.SIGCONT)
            job.paused = False

    def _housekeeping(self) -> None:
        last_lru = 0.0
        while True:
            time.sleep(2)
            now = time.time()
            ahead = transcode_settings()["ahead_segments"]
            with self.lock:
                for key, job in list(self.jobs.items()):
                    running = job.proc is not None and job.proc.poll() is None
                    if now - job.last_access > self.IDLE_KILL_S:
                        _kill(job)
                        del self.jobs[key]
                        continue
                    if running and not job.paused and self._encoder_position(job) - job.last_request_seg > ahead:
                        self._pause(job)  # far enough ahead of the viewer — save GPU/CPU
            if now - last_lru > 600:
                last_lru = now
                try:
                    enforce_cache_limit()
                except Exception:
                    log.exception("cache cleanup failed")


def _touch(p: Path) -> None:
    try:
        os.utime(p, None)
    except OSError:
        pass


def _kill(job: EncodeJob) -> None:
    if job.proc and job.proc.poll() is None:
        if job.paused and hasattr(signal, "SIGCONT"):
            os.kill(job.proc.pid, signal.SIGCONT)
        job.proc.kill()
        try:
            job.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    job.paused = False


def cache_usage() -> dict:
    total, entries = 0, 0
    if settings.transcode_dir.exists():
        for d in settings.transcode_dir.iterdir():
            if d.is_dir():
                entries += 1
                for f in d.rglob("*"):
                    if f.is_file():
                        total += f.stat().st_size
    return {"bytes": total, "entries": entries, "limit_bytes": int(transcode_settings()["cache_gb"] * 1e9)}


def enforce_cache_limit() -> int:
    """Evict least-recently-watched transcodes until under the size limit. Pinned (optimized) items stay."""
    limit = transcode_settings()["cache_gb"] * 1e9
    dirs = []
    total = 0
    for d in settings.transcode_dir.iterdir() if settings.transcode_dir.exists() else []:
        if not d.is_dir():
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        total += size
        dirs.append((d.stat().st_mtime, d, size, (d / ".pinned").exists()))
    removed = 0
    active = {str(Path(j.dir).parent) for j in segments.jobs.values() if j.proc and j.proc.poll() is None}
    for _, d, size, pinned in sorted(dirs):
        if total <= limit:
            break
        if pinned or str(d) in active:
            continue
        shutil.rmtree(d, ignore_errors=True)
        total -= size
        removed += 1
    return removed


def clear_cache() -> None:
    segments.stop_all()
    if settings.transcode_dir.exists():
        for d in settings.transcode_dir.iterdir():
            shutil.rmtree(d, ignore_errors=True)


segments = SegmentManager()


# ---- optimize ahead ----------------------------------------------------------------------------------

def optimize(item: dict, meta: dict, source: Path, profiles: list[str], ctx=None) -> str:
    """Pre-encode whole rungs into the cache so playback never waits (pinned against eviction)."""
    done = []
    for n, profile in enumerate(profiles):
        if profile not in available_profiles(meta):
            continue
        d = cache_dir_for(item, profile, source)
        d.mkdir(parents=True, exist_ok=True)
        (d.parent / ".pinned").touch()
        total = len(fixed_segments(item["duration"] or meta.get("duration") or 0))
        if total and (d / f"seg_{total - 1}.ts").exists():
            done.append(profile)
            continue
        method = active_hwaccel()
        in_args, v_args = build_video_args(meta, profile, method, cuda_decode=cuda_decode_available())
        cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-y", *in_args, "-i", str(source),
               "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn", *v_args, "-c:a", "aac", "-ac", "2",
               "-b:a", f"{PROFILES[profile][2]}k", "-f", "hls", "-hls_time", f"{SEGMENT}",
               "-hls_playlist_type", "vod", "-hls_flags", "temp_file+independent_segments",
               "-hls_segment_filename", str(d / "seg_%d.ts"), str(d / "full.m3u8")]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            time.sleep(2)
            if ctx and total:
                have = len(list(d.glob("seg_*.ts")))
                ctx.progress((n + have / total) / len(profiles), f"Optimizing {item['title']} {profile}: {have}/{total}")
        if proc.returncode == 0:
            done.append(profile)
    return f"Optimized {item['title']}: {', '.join(done) or 'nothing'}"


# ---- trickplay (scrub previews) --------------------------------------------------------------------

TRICK_INTERVAL = 10
TRICK_COLS = 10
TRICK_W = 240


def trickplay_dir(item_id: int) -> Path:
    return settings.cache_dir / "trickplay" / str(item_id)


def trickplay_info(item: dict) -> dict | None:
    f = trickplay_dir(item["id"]) / "index.json"
    return json.loads(f.read_text()) if f.exists() else None


def build_trickplay(item: dict, meta: dict, source: Path) -> dict | None:
    d = trickplay_dir(item["id"])
    d.mkdir(parents=True, exist_ok=True)
    w, h = _target_size(meta, 9999)
    th = _even(TRICK_W * h / w) if w else 136
    cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-skip_frame", "nokey", "-i", str(source),
           "-vf", f"fps=1/{TRICK_INTERVAL},scale={TRICK_W}:{th},tile={TRICK_COLS}x{TRICK_COLS}",
           "-fps_mode", "vfr", "-q:v", "6", str(d / "sprite_%03d.jpg")]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return None
    sprites = sorted(p.name for p in d.glob("sprite_*.jpg"))
    if r.returncode != 0 or not sprites:
        return None
    dur = item["duration"] or meta.get("duration") or 0
    info = {"interval": TRICK_INTERVAL, "cols": TRICK_COLS, "rows": TRICK_COLS, "width": TRICK_W, "height": th,
            "count": int(dur // TRICK_INTERVAL) + 1, "sprites": sprites}
    (d / "index.json").write_text(json.dumps(info))
    return info


# ---- other streams ----------------------------------------------------------------------------------

def progressive_cmd(source: Path, meta: dict, start: float, profile: str) -> list[str]:
    """Fallback for browsers without MSE/HLS support: one fragmented MP4 stream from ?start=."""
    method = active_hwaccel()
    in_args, v_args = build_video_args(meta, profile, method, cuda_decode=cuda_decode_available())
    _, _, ak = PROFILES[profile]
    return [settings.ffmpeg, "-v", "error", "-nostdin", *in_args, "-ss", f"{max(start, 0):.3f}", "-i", str(source),
            "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn", *v_args, "-c:a", "aac", "-ac", "2", "-b:a", f"{ak}k",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]


def audio_cmd(source: Path, start: float) -> list[str]:
    return [settings.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{max(start, 0):.3f}", "-i", str(source),
            "-vn", "-c:a", "aac", "-b:a", "256k", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]


def subtitle_vtt(source: Path, stream_index: int | None = None, sidecar: Path | None = None) -> bytes:
    src = sidecar or source
    cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-i", str(src)]
    if stream_index is not None and sidecar is None:
        cmd += ["-map", f"0:{stream_index}"]
    cmd += ["-f", "webvtt", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    return r.stdout
