"""Direct play, quality ladder, HLS transcode + stream-copy, caching, captions, trickplay."""

import json
import time

from conftest import item_by_title, wait_jobs

from paddock import db, transcode, transcribe


def test_direct_play_with_range(admin):
    b = item_by_title(admin, "Big Buck")
    plan = admin.get(f"/api/items/{b['id']}/playback").json()
    assert plan["mode"] == "direct" and plan["default_quality"] == "original"
    ids = [q["id"] for q in plan["qualities"]]
    assert ids[0] == "auto" and "original" in ids and "360p" in ids
    assert "1080p" not in ids  # never upscale a 360p source
    r = admin.get(plan["url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


def test_hevc_mkv_needs_transcode_for_browsers(admin):
    m = item_by_title(admin, "The Matrix")
    plan = admin.get(f"/api/items/{m['id']}/playback").json()
    assert plan["mode"] == "transcode" and "hevc" in plan["reason"]
    assert not any(q["id"] == "original" for q in plan["qualities"])  # browser can't decode HEVC here
    # a phone that decodes HEVC gets Original via stream-copy
    phone = admin.get(f"/api/items/{m['id']}/playback", params={"codecs": "h264,hevc,aac,mp3,opus"}).json()
    assert any(q["id"] == "original" and q["type"] == "hls" for q in phone["qualities"])
    # ExoPlayer also plays MKV directly
    exo = admin.get(f"/api/items/{m['id']}/playback", params={"codecs": "h264,hevc,aac,mkv"}).json()
    assert exo["mode"] == "direct"


def test_hls_master_and_transcoded_segment(admin):
    m = item_by_title(admin, "The Matrix")
    master = admin.get(f"/api/items/{m['id']}/hls/master.m3u8").text
    assert "#EXT-X-STREAM-INF" in master and "360p/index.m3u8" in master and "original" not in master
    idx = admin.get(f"/api/items/{m['id']}/hls/360p/index.m3u8").text
    assert idx.count("seg_") == 2 and "#EXT-X-ENDLIST" in idx  # 8 s → two 6 s segments
    t0 = time.time()
    r = admin.get(f"/api/items/{m['id']}/hls/360p/seg_1.ts")
    first = time.time() - t0
    assert r.status_code == 200 and r.content[:1] == b"\x47"  # MPEG-TS sync byte
    t0 = time.time()
    r2 = admin.get(f"/api/items/{m['id']}/hls/360p/seg_1.ts")
    assert r2.status_code == 200 and r2.content == r.content
    assert time.time() - t0 < first  # served from cache
    r0 = admin.get(f"/api/items/{m['id']}/hls/360p/seg_0.ts")  # seek backwards restarts encoder correctly
    assert r0.status_code == 200 and r0.content[:1] == b"\x47"


def test_original_stream_copy_segments(admin):
    m = item_by_title(admin, "The Matrix")
    idx = admin.get(f"/api/items/{m['id']}/hls/original/index.m3u8").text
    assert "seg_0.ts" in idx
    r = admin.get(f"/api/items/{m['id']}/hls/original/seg_0.ts")
    assert r.status_code == 200 and r.content[:1] == b"\x47"
    probe = transcode.subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                      "-i", "pipe:0"], input=r.content, capture_output=True)
    assert b"hevc" in probe.stdout  # video bitstream untouched
    assert b"aac" in probe.stdout


def test_progressive_fallback(admin):
    m = item_by_title(admin, "The Matrix")
    with admin.stream("GET", f"/api/items/{m['id']}/stream.mp4", params={"start": 2, "profile": "360p"}) as r:
        head = next(r.iter_bytes())
    assert r.status_code == 200 and b"ftyp" in head[:64]


def test_red_plays_from_proxy(admin):
    clip = admin.get("/api/items", params={"kind": "red_clip"}).json()[0]
    plan = admin.get(f"/api/items/{clip['id']}/playback").json()
    assert plan["mode"] == "transcode" and plan["source"] == "proxy"
    r = admin.get(f"/api/items/{clip['id']}/hls/360p/seg_0.ts")
    assert r.status_code == 200


def test_progress_and_continue_watching(admin):
    b = item_by_title(admin, "Big Buck")
    admin.post(f"/api/items/{b['id']}/progress", json={"position": 2, "duration": 100})
    db.x("UPDATE progress SET position=30 WHERE item_id=?", (b["id"],))
    rows = {r["key"]: r for r in admin.get("/api/home").json()["rows"]}
    assert rows["continue"]["items"][0]["id"] == b["id"]
    plan = admin.get(f"/api/items/{b['id']}/playback").json()
    assert plan["resume_at"] == 30
    assert admin.get(f"/api/items/{b['id']}").json()["progress"]["completed"] is False  # a real boolean, not 0
    admin.post(f"/api/items/{b['id']}/progress", json={"position": 98, "duration": 100})
    assert "continue" not in {r["key"] for r in admin.get("/api/home").json()["rows"]}


def test_cache_eviction_respects_limit(admin):
    usage = transcode.cache_usage()
    assert usage["bytes"] > 0
    db.set_setting("transcode", {"cache_gb": 0.000001})
    removed = transcode.enforce_cache_limit()
    db.set_setting("transcode", {})
    assert removed >= 1


def test_trickplay(admin):
    m = item_by_title(admin, "The Matrix")
    item = db.q1("SELECT * FROM items WHERE id=?", (m["id"],))
    info = transcode.build_trickplay(item, json.loads(item["meta"]), transcode.Path(item["path"]))
    assert info and info["sprites"]
    plan = admin.get(f"/api/items/{m['id']}/playback").json()
    assert plan["trickplay"]["sprites"]
    assert admin.get(f"/api/items/{m['id']}/trickplay/{info['sprites'][0]}").status_code == 200
    assert admin.get(f"/api/items/{m['id']}/trickplay/..%2Fx.jpg").status_code == 404


def test_live_captions(admin, monkeypatch):
    v = item_by_title(admin, "launch-day")
    calls = []

    def fake_chunk(item, idx):
        calls.append(idx)
        return [[idx * transcribe.CHUNK + 0.5, idx * transcribe.CHUNK + 2.5, "the turbo spools up nicely"]]

    monkeypatch.setattr(transcribe, "transcribe_chunk", fake_chunk)
    transcribe.save_config({"engine": "server"})
    try:
        first = admin.get(f"/api/items/{v['id']}/captions/live", params={"at": 0}).json()
        assert first["enabled"] and first["total_chunks"] == 1
        deadline = time.time() + 10
        data = first
        while time.time() < deadline and not data["complete"]:
            time.sleep(0.1)
            data = admin.get(f"/api/items/{v['id']}/captions/live", params={"at": 0}).json()
        assert data["complete"] and data["cues"][0]["text"] == "the turbo spools up nicely"
        vtt = admin.get(f"/api/items/{v['id']}/captions.vtt").text
        assert vtt.startswith("WEBVTT") and "00:00:00.500 --> 00:00:02.500" in vtt
        # spoken words are now searchable
        hits = admin.get("/api/items", params={"query": "spools"}).json()
        assert hits and hits[0]["id"] == v["id"]
    finally:
        transcribe.save_config({"engine": "none"})


def test_hw_detection_reports(admin):
    hw = admin.post("/api/admin/transcode/detect").json()
    assert {"working", "errors", "decode"} <= hw.keys() and isinstance(hw["active"], str) and hw["active"]


def test_hybrid_nvdec_pipeline_args(monkeypatch):
    monkeypatch.setattr(transcode, "WINDOWS", True)
    meta = {"vcodec": "hevc", "pix_fmt": "yuv420p10le", "width": 3840, "height": 2160, "fps": 30, "hdr": False}
    i, v = transcode.build_video_args(meta, "1080p", "qsv", cuda_decode=True)
    assert i[:2] == ["-hwaccel", "cuda"] and "scale_cuda" in v[1] and "h264_qsv" in v
    i, v = transcode.build_video_args(meta, "1080p", "none", cuda_decode=True)
    assert "scale_cuda" in v[1] and "libx264" in v
    # formats NVDEC can't decode never go to the NVIDIA decoder
    for codec, pf in (("hevc", "yuv422p10le"), ("hevc", "yuv444p"), ("hevc", "yuv420p12le"), ("h264", "yuv420p10le")):
        i, _ = transcode.build_video_args(meta | {"vcodec": codec, "pix_fmt": pf}, "1080p", "qsv", cuda_decode=True)
        assert "cuda" not in i


def test_hw_errors_are_explained():
    nv = ("[h264_nvenc @ 0000021] Driver does not support the required nvenc API version. Required: 13.1 Found: 12.2\n"
          "[h264_nvenc @ 0000021] The minimum required Nvidia driver for nvenc is 610.00 or newer\n"
          "[vost#0:0/h264_nvenc @ 0000022] Error while opening encoder - maybe incorrect parameters\n")
    assert "too old" in transcode._explain(nv) and "610.00" in transcode._explain(nv)
    qsv = "[h264_qsv @ 0x3a] Error creating a MFX session: -9.\n[vost#0:0/h264_qsv @ 0x3b] Error while opening encoder\n"
    assert transcode._explain(qsv) == "Error creating a MFX session: -9."


def test_whisper_paths_survive_ffmpeg_escaping():
    # ffmpeg unescapes filter options twice, so a drive colon needs two backslashes
    assert transcribe._filter_path(r"C:\Users\me\ggml.bin") == "C\\\\:/Users/me/ggml.bin"
    assert transcribe._filter_path("/data/models/ggml.bin") == "/data/models/ggml.bin"
    assert transcribe._filter_path("/m/a,b.bin") == "/m/a\\,b.bin"


def test_whisper_model_download_is_checksum_verified(admin, monkeypatch):
    import hashlib

    good = b"pretend ggml weights"
    name = "ggml-test.en.bin"
    monkeypatch.setattr(transcribe, "WHISPER_MODELS", {name: {
        "label": "Test", "note": "", "size": len(good), "sha256": hashlib.sha256(good).hexdigest()}})

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self, n): yield self.body

    dest = transcribe.models_dir() / name
    try:
        # tampered download: same length, different bytes -> rejected, nothing left behind
        monkeypatch.setattr(transcribe.httpx, "stream", lambda *a, **k: Resp(good.upper()))
        try:
            transcribe.download_whisper_model(name)
            raise AssertionError("tampered model was accepted")
        except RuntimeError as e:
            assert "checksum" in str(e)
        assert not dest.exists() and not list(transcribe.models_dir().glob("*.part"))

        monkeypatch.setattr(transcribe.httpx, "stream", lambda *a, **k: Resp(good))
        transcribe.download_whisper_model(name)
        cfg = transcribe.config()
        assert dest.read_bytes() == good and cfg["ffmpeg_model"] == str(dest)
        assert cfg["engine"] == "ffmpeg" and cfg["translate"] is False   # English-only model can't translate
        # a config copied from another machine (Linux container path) finds the same model here
        transcribe.save_config({"ffmpeg_model": f"/data/models/{name}"})
        assert transcribe.config()["ffmpeg_model"] == str(dest)
        st = admin.get("/api/admin/stt").json()["status"]
        assert st["models"][0]["installed"] and st["models"][0]["active"]
        assert admin.post("/api/admin/stt/whisper-model", json={"name": "nope.bin"}).status_code == 400
    finally:
        dest.unlink(missing_ok=True)
        transcribe.save_config({"engine": "none", "ffmpeg_model": ""})


def test_up_next_continues_the_show_then_fills(admin):
    pilot = item_by_title(admin, "Pilot")
    nxt = admin.get(f"/api/items/{pilot['id']}/up_next").json()
    assert nxt[0]["title"] == "Turbo Time"            # next episode first
    assert pilot["id"] not in [c["id"] for c in nxt]
    assert len(nxt) >= 3 and all(c["kind"] in ("video", "red_clip", "braw_clip") for c in nxt)
    song = item_by_title(admin, "Launch Control")
    assert all(c["kind"] == "audio" for c in admin.get(f"/api/items/{song['id']}/up_next").json())
