"""Live and offline transcription (speech → captions), fully local.

Engines:
  * "server" — an OpenAI-compatible speech-to-text server on your LAN, e.g.
               speaches / faster-whisper-server (GPU image), or whisper.cpp's server.
               POST {url}/audio/transcriptions with response_format=verbose_json.
  * "ffmpeg" — ffmpeg's built-in `whisper` filter (FFmpeg 8+, whisper.cpp) with a
               local ggml model file. No extra service; uses the GPU if ffmpeg can.

Live mode: while someone watches with captions on, the player asks for cues
around the playhead; Paddock transcribes 20-second chunks just ahead of it, most
urgent first, and caches every chunk. When a file is fully transcribed the text is
indexed, so search finds things that were *said* in your footage.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import db
from .config import settings

log = logging.getLogger("paddock.transcribe")

CHUNK = 20.0

DEFAULTS = {
    "engine": "none",                               # none | server | ffmpeg
    "server_url": "http://speaches:8000/v1",
    "server_model": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "ffmpeg_model": "",                             # empty = best model downloaded into <data>/models
    "ffmpeg_vad_model": "",
    "language": "auto",
    "translate": False,
    "auto_libraries": [],                           # library ids to transcribe in the background
}


# whisper.cpp models offered for one-click download, best first. Sizes and SHA-256 are pinned from the
# repository's published LFS metadata; a download that doesn't match exactly is thrown away.
WHISPER_REPO = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
WHISPER_MODELS = {
    "ggml-large-v3-turbo.bin": {
        "label": "Large v3 Turbo", "note": "Most accurate, all languages. Best with a GPU.",
        "size": 1624555275, "sha256": "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"},
    "ggml-large-v3-turbo-q5_0.bin": {
        "label": "Large v3 Turbo (compressed)", "note": "Nearly as accurate at a third of the size. Recommended.",
        "size": 574041195, "sha256": "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"},
    "ggml-small.bin": {
        "label": "Small", "note": "Quick on a CPU, all languages.",
        "size": 487601967, "sha256": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"},
    "ggml-base.en.bin": {
        "label": "Base (English)", "note": "Fastest. English only, can't translate.",
        "size": 147964211, "sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"},
}


def models_dir() -> Path:
    return settings.data_dir / "models"


def _resolve_model(path: str) -> str:
    """The configured model if it exists, else the same file in Paddock's models folder (configs copied
    between machines), else the best model downloaded there."""
    if path and Path(path).is_file():
        return path
    d = models_dir()
    if path and (d / Path(path).name).is_file():
        return str(d / Path(path).name)
    for name in WHISPER_MODELS:
        if (d / name).is_file():
            return str(d / name)
    return path


def config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(db.get_setting("stt", {}))
    cfg["ffmpeg_model"] = _resolve_model(cfg.get("ffmpeg_model") or "")
    return cfg


def save_config(update: dict) -> dict:
    cfg = db.get_setting("stt", {})
    for k in DEFAULTS:
        if k in update and update[k] is not None:
            cfg[k] = update[k]
    if cfg.get("engine", "none") not in ("none", "server", "ffmpeg"):
        raise ValueError("engine must be none, server or ffmpeg")
    db.set_setting("stt", cfg)
    return config()


def enabled() -> bool:
    return config()["engine"] != "none"


# ---- storage ------------------------------------------------------------------------------

def _store_path(item_id: int) -> Path:
    return settings.cache_dir / "captions" / f"{item_id}.json"


def transcript_text_path(item_id: int) -> Path:
    return settings.cache_dir / "captions" / f"{item_id}.txt"


_store_lock = threading.Lock()


def load(item_id: int) -> dict:
    p = _store_path(item_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"chunks": {}, "complete": False}


def _save_chunk(item: dict, idx: int, cues: list[list]) -> dict:
    with _store_lock:
        data = load(item["id"])
        data["chunks"][str(idx)] = cues
        data["chunk_seconds"] = CHUNK
        total = n_chunks(item)
        data["total_chunks"] = total
        if total and all(str(i) in data["chunks"] for i in range(total)):
            data["complete"] = True
        p = _store_path(item["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)
    if data["complete"]:
        _finish(item, data)
    return data


def _finish(item: dict, data: dict) -> None:
    text = " ".join(c[2] for i in sorted(data["chunks"], key=int) for c in data["chunks"][i])
    transcript_text_path(item["id"]).write_text(text, encoding="utf-8")
    db.fts_index(item["id"])  # spoken words become searchable
    db.audit("system", "transcript.complete", item=item["id"], words=len(text.split()))


def n_chunks(item: dict) -> int:
    dur = item.get("duration") or json.loads(item.get("meta") or "{}").get("duration") or 0
    return int(dur // CHUNK) + (1 if dur % CHUNK > 0.5 else 0)


def cues(item_id: int, start: float = 0, end: float = 1e12) -> list[dict]:
    data = load(item_id)
    out = []
    for i in sorted(data["chunks"], key=int):
        for s, e, t in data["chunks"][i]:
            if e >= start and s <= end:
                out.append({"start": round(s, 3), "end": round(e, 3), "text": t})
    return out


def to_vtt(item_id: int) -> str:
    def ts(x: float) -> str:
        h, rem = divmod(x, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"

    lines = ["WEBVTT", ""]
    for n, c in enumerate(cues(item_id), 1):
        lines += [str(n), f"{ts(c['start'])} --> {ts(c['end'])}", c["text"], ""]
    return "\n".join(lines)


# ---- engines ---------------------------------------------------------------------------------

def _source(item: dict) -> Path:
    from .scanner import safe_item_path

    meta = json.loads(item.get("meta") or "{}")
    if item["kind"] == "red_clip":
        return Path(meta["proxy"])
    return safe_item_path(item)


def _extract_wav(src: Path, start: float, dur: float) -> bytes:
    r = subprocess.run([settings.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                       capture_output=True, timeout=120)
    return r.stdout


def _clean(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    # Whisper's classic hallucinations on silence
    if t.lower() in {"you", "thank you.", "thanks for watching!", "thank you for watching.", ".", "..."}:
        return ""
    return t


def _via_server(cfg: dict, wav: bytes, offset: float) -> list[list]:
    data = {"model": cfg["server_model"], "response_format": "verbose_json", "temperature": "0"}
    if cfg.get("language") and cfg["language"] != "auto":
        data["language"] = cfg["language"]
    endpoint = "translations" if cfg.get("translate") else "transcriptions"
    r = httpx.post(f"{cfg['server_url'].rstrip('/')}/audio/{endpoint}", data=data,
                   files={"file": ("chunk.wav", wav, "audio/wav")}, timeout=300)
    r.raise_for_status()
    body = r.json()
    out = []
    for seg in body.get("segments") or []:
        t = _clean(seg.get("text", ""))
        if t and seg.get("no_speech_prob", 0) < 0.8:
            out.append([offset + float(seg["start"]), offset + float(seg["end"]), t])
    if not out and body.get("text") and _clean(body["text"]):
        out.append([offset, offset + CHUNK, _clean(body["text"])])
    return out


_SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _filter_path(path: str | Path) -> str:
    """Escape a path for a filter option inside -af. ffmpeg unescapes twice (filter graph, then the
    option list), so Windows drive colons need two levels: C:/x -> C\\\\:/x."""
    p = str(path).replace("\\", "/")
    p = re.sub(r"([:'])", r"\\\1", p)
    return re.sub(r"([\\'\[\],;])", r"\\\1", p)


def _via_ffmpeg(cfg: dict, src: Path, start: float, dur: float) -> list[list]:
    if not cfg["ffmpeg_model"] or not Path(cfg["ffmpeg_model"]).is_file():
        raise RuntimeError("No whisper model downloaded yet (Admin > Transcription)")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out.srt"
        opts = [f"model={_filter_path(cfg['ffmpeg_model'])}", f"language={cfg.get('language') or 'auto'}",
                f"queue={int(CHUNK)}", f"destination={_filter_path(out)}", "format=srt"]
        if cfg.get("translate"):
            opts.append("translate=1")
        if cfg.get("ffmpeg_vad_model"):
            opts.append(f"vad_model={_filter_path(cfg['ffmpeg_vad_model'])}")
        cmd = [settings.ffmpeg, "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
               "-vn", "-af", "aresample=16000,whisper=" + ":".join(opts), "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0:
            # don't cache a failure as "nothing was said" — the worker retries failed chunks later
            raise RuntimeError(r.stderr.decode(errors="ignore").strip()[-300:] or f"ffmpeg exited {r.returncode}")
        if not out.exists():
            return []
        cues_: list[list] = []
        for block in out.read_text(encoding="utf-8", errors="ignore").split("\n\n"):
            m = _SRT_TIME.search(block)
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
            e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
            text = _clean(" ".join(block[m.end():].strip().splitlines()))
            if text:
                cues_.append([start + s, start + e, text])
        return cues_


def transcribe_chunk(item: dict, idx: int) -> list[list]:
    cfg = config()
    src = _source(item)
    start = idx * CHUNK
    if cfg["engine"] == "server":
        wav = _extract_wav(src, start, CHUNK)
        if len(wav) < 1000:
            return []
        return _via_server(cfg, wav, start)
    if cfg["engine"] == "ffmpeg":
        return _via_ffmpeg(cfg, src, start, CHUNK)
    return []


# ---- scheduler: live chunks first, background after -------------------------------------------

class Worker:
    LIVE, BACKGROUND = 0, 10

    def __init__(self):
        self.heap: list[tuple[int, float, int, int]] = []   # (priority, enqueued, item_id, chunk)
        self.queued: set[tuple[int, int]] = set()
        self.failed: dict[tuple[int, int], float] = {}
        self.cv = threading.Condition()
        self.thread: threading.Thread | None = None
        self.current: tuple[int, int] | None = None

    def ensure_started(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="transcriber")
            self.thread.start()

    def request(self, item: dict, chunks: list[int], priority: int) -> None:
        self.ensure_started()
        have = load(item["id"])["chunks"]
        with self.cv:
            for rank, c in enumerate(chunks):
                key = (item["id"], c)
                if str(c) in have or key == self.current:
                    continue
                if time.time() - self.failed.get(key, 0) < 120:
                    continue
                if key in self.queued and priority != self.LIVE:
                    continue
                self.queued.add(key)
                heapq.heappush(self.heap, (priority + rank, time.time(), item["id"], c))
            self.cv.notify()

    def pending(self, item_id: int) -> int:
        with self.cv:
            return sum(1 for k in self.queued if k[0] == item_id)

    def _run(self) -> None:
        while True:
            with self.cv:
                while not self.heap:
                    self.cv.wait()
                _, _, item_id, c = heapq.heappop(self.heap)
                key = (item_id, c)
                if key not in self.queued:
                    continue
                self.queued.discard(key)
                self.current = key
            try:
                item = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
                if item and str(c) not in load(item_id)["chunks"]:
                    _save_chunk(item, c, transcribe_chunk(item, c))
            except Exception as e:  # noqa: BLE001
                log.warning("transcription failed for item %s chunk %s: %s", item_id, c, e)
                self.failed[key] = time.time()
            finally:
                self.current = None


worker = Worker()


def live(item: dict, at: float, window_before: float = 15, window_after: float = 120) -> dict:
    """Cues around the playhead; schedules transcription of what's coming up next."""
    total = n_chunks(item)
    if enabled() and total:
        first = max(int(at // CHUNK), 0)
        upcoming = [c for c in range(first, min(first + int(window_after // CHUNK) + 1, total))]
        worker.request(item, upcoming, Worker.LIVE)
    data = load(item["id"])
    return {
        "enabled": enabled(),
        "cues": cues(item["id"], at - window_before, at + window_after),
        "complete": data.get("complete", False),
        "transcribed_chunks": len(data["chunks"]),
        "total_chunks": total,
        "pending": worker.pending(item["id"]),
        "chunk_seconds": CHUNK,
    }


def transcribe_all(item: dict) -> int:
    total = n_chunks(item)
    worker.request(item, list(range(total)), Worker.BACKGROUND)
    return total


def server_status() -> dict:
    cfg = config()
    out = {"engine": cfg["engine"], "ok": False}
    if cfg["engine"] == "server":
        try:
            r = httpx.get(f"{cfg['server_url'].rstrip('/')}/models", timeout=5)
            r.raise_for_status()
            out["models"] = [m.get("id") for m in r.json().get("data", [])][:50]
            out["ok"] = True
        except (httpx.HTTPError, ValueError) as e:
            out["error"] = str(e)[:200]
    elif cfg["engine"] == "ffmpeg":
        try:
            filters = subprocess.run([settings.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                                     timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            filters = ""
        out["ffmpeg_has_whisper"] = " whisper " in filters
        out["model_present"] = bool(cfg["ffmpeg_model"]) and Path(cfg["ffmpeg_model"]).is_file()
        out["ok"] = out["ffmpeg_has_whisper"] and out["model_present"]
        if not out["ffmpeg_has_whisper"]:
            out["error"] = "This ffmpeg was built without the whisper filter (needs FFmpeg 8+ with --enable-whisper)."
        elif not out["model_present"]:
            out["error"] = "Whisper is built in, but no speech model is downloaded yet. Pick one below."
    out["models"] = whisper_models(cfg)
    with worker.cv:
        out["queue"] = len(worker.queued)
    return out


def whisper_models(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or config()
    active = Path(cfg["ffmpeg_model"]).name if cfg.get("ffmpeg_model") else ""
    return [{"name": n, "label": m["label"], "note": m["note"], "size": m["size"],
             "installed": (models_dir() / n).is_file(), "active": n == active} for n, m in WHISPER_MODELS.items()]


def download_whisper_model(name: str, ctx=None) -> str:
    """Fetch a whisper.cpp model into <data>/models, verify it against the pinned SHA-256, and switch to it."""
    import shutil

    spec = WHISPER_MODELS.get(name)
    if not spec:
        raise ValueError("Unknown model")
    d = models_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / name
    if not (dest.is_file() and dest.stat().st_size == spec["size"]):
        if shutil.disk_usage(d).free < spec["size"] * 1.1:
            raise RuntimeError(f"Not enough free space in {d}")
        tmp = d / (name + ".part")
        digest = hashlib.sha256()
        got = 0
        try:
            with httpx.stream("GET", f"{WHISPER_REPO}/{name}", follow_redirects=True,
                              timeout=httpx.Timeout(30, read=120)) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
                        got += len(chunk)
                        if got > spec["size"]:
                            raise RuntimeError("Download is larger than expected")
                        if ctx:
                            ctx.progress(got / spec["size"], f"{spec['label']}: {got >> 20} of {spec['size'] >> 20} MB")
            if got != spec["size"] or digest.hexdigest() != spec["sha256"]:
                raise RuntimeError("Download didn't match the published checksum, so it was discarded")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
    use_whisper_model(name)
    return f"{spec['label']} ready ({spec['size'] >> 20} MB, checksum verified)"


def use_whisper_model(name: str) -> dict:
    if name not in WHISPER_MODELS or not (models_dir() / name).is_file():
        raise ValueError("That model isn't downloaded")
    cfg = db.get_setting("stt", {})
    cfg["ffmpeg_model"] = str(models_dir() / name)
    if cfg.get("engine", "none") == "none":
        cfg["engine"] = "ffmpeg"
    if name.endswith(".en.bin"):
        cfg["translate"] = False
    db.set_setting("stt", cfg)
    return config()


def download_server_model(model: str) -> str:
    """Ask a speaches server to fetch a model (POST /v1/models/{id})."""
    cfg = config()
    r = httpx.post(f"{cfg['server_url'].rstrip('/')}/models/{model}", timeout=httpx.Timeout(30, read=3600))
    if r.status_code >= 300:
        raise RuntimeError(f"Server replied {r.status_code}: {r.text[:200]}")
    return f"Downloaded {model}"
