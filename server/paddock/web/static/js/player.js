// YouTube-style video player: quality ladder (Auto / Original / rungs), hls.js or native HLS,
// direct play, captions (embedded, sidecar, live AI transcription), scrub previews, keyboard
// shortcuts, PiP, theater, resume & progress sync, stats overlay.

import { api } from "./api.js";
import { h, icon, iconBtn, fmtDuration, toast } from "./ui.js";

export function clientCodecs() {
  const v = document.createElement("video");
  const mse = window.MediaSource || window.ManagedMediaSource;
  const can = (t) => (mse && mse.isTypeSupported?.(t)) || v.canPlayType(t) === "probably";
  const out = [];
  if (can('video/mp4; codecs="avc1.640028"')) out.push("h264");
  if (can('video/mp4; codecs="hvc1.1.6.L120.90"') || can('video/mp4; codecs="hev1.1.6.L120.90"')) out.push("hevc");
  if (can('video/mp4; codecs="av01.0.08M.08"')) out.push("av1");
  if (can('video/webm; codecs="vp9"')) out.push("vp9");
  if (can('video/webm; codecs="vp8"')) out.push("vp8");
  if (can('audio/mp4; codecs="mp4a.40.2"')) out.push("aac");
  if (v.canPlayType("audio/mpeg")) out.push("mp3");
  if (can('audio/webm; codecs="opus"') || can('audio/mp4; codecs="opus"')) out.push("opus");
  if (v.canPlayType("audio/flac")) out.push("flac");
  if (can('audio/webm; codecs="vorbis"')) out.push("vorbis");
  return out;
}

const nativeHls = () => !!document.createElement("video").canPlayType("application/vnd.apple.mpegurl");
const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

export class Player {
  constructor(item, plan, opts = {}) {
    this.item = item;
    this.plan = plan;
    this.opts = opts;
    this.quality = localStorage.getItem("quality") || plan.default_quality || "auto";
    if (!plan.qualities.some((q) => q.id === this.quality)) this.quality = plan.default_quality;
    this.captionMode = localStorage.getItem("captions") || "off"; // off | live | track:<n>
    this.hls = null;
    this.offset = 0; // for progressive fallback streams
    this.liveCues = [];
    this.lastSaved = 0;
    this.build();
    this.load(this.quality, plan.resume_at || 0, true);
  }

  // ---------- DOM ----------
  build() {
    const isAudio = this.item.kind === "audio";
    this.video = h("video", { playsinline: true, preload: "metadata", crossorigin: "use-credentials" });
    this.root = h("div", { class: `player paused ${isAudio ? "audio" : ""}`, tabindex: "0" });
    if (this.item.has_thumb) this.poster = h("img", { class: "poster-img", src: `/api/items/${this.item.id}/thumb`, alt: "" });
    this.bigPlay = h("button", { class: "big-play", "aria-label": "Play", onclick: () => this.toggle() }, icon("play"));
    this.flash = h("div", { class: "flash" });
    this.hintL = h("div", { class: "seek-hint l" }, "« 10s");
    this.hintR = h("div", { class: "seek-hint r" }, "10s »");
    this.captionBox = h("div", { class: "captions", "aria-live": "off" });
    this.titleBar = h("div", { class: "title-bar" }, this.item.title);

    // progress bar
    this.buf = h("div", { class: "buf" });
    this.played = h("div", { class: "played" });
    this.hoverFill = h("div", { class: "hover-fill" });
    this.knob = h("div", { class: "knob" });
    this.prevFrame = h("div", { class: "frame" });
    this.prevTime = h("div");
    this.preview = h("div", { class: "preview" }, this.plan.trickplay ? this.prevFrame : null, this.prevTime);
    this.bar = h("div", { class: "bar", role: "slider", "aria-label": "Seek", tabindex: "-1" },
      h("div", { class: "rail" }, this.buf, this.hoverFill, this.played), this.knob, this.preview);

    this.playBtn = iconBtn("play", "Play (k)", () => this.toggle());
    this.muteBtn = iconBtn("volume", "Mute (m)", () => { this.video.muted = !this.video.muted; });
    this.vol = h("input", { type: "range", min: 0, max: 1, step: 0.05, value: localStorage.getItem("volume") ?? 1, "aria-label": "Volume" });
    this.vol.addEventListener("input", () => { this.video.volume = +this.vol.value; this.video.muted = false; localStorage.setItem("volume", this.vol.value); });
    this.time = h("span", { class: "time" }, "0:00 / 0:00");
    this.ccBtn = iconBtn("cc", "Captions (c)", () => this.toggleCaptions());
    this.gearBtn = iconBtn("settings", "Settings", (e) => { e.stopPropagation(); this.toggleSettings(); });
    this.qTag = h("span", { class: "quality-tag" });
    this.gearBtn.append(this.qTag);
    const pipBtn = document.pictureInPictureEnabled && !isAudio ? iconBtn("pip", "Picture-in-picture (i)", () => this.pip()) : null;
    const theaterBtn = this.opts.onTheater && !isAudio ? iconBtn("theater", "Theater mode (t)", () => this.opts.onTheater()) : null;
    this.fsBtn = !isAudio ? iconBtn("fullscreen", "Fullscreen (f)", () => this.fullscreen()) : null;
    const nextBtn = this.opts.onNext ? iconBtn("next", "Next (Shift+N)", () => this.opts.onNext()) : null;
    this.controls = h("div", { class: "controls" }, this.bar,
      h("div", { class: "buttons" }, this.playBtn, nextBtn, h("div", { class: "vol" }, this.muteBtn, this.vol), this.time,
        h("div", { class: "spacer" }), this.plan.subtitles.length || this.plan.live_captions?.enabled || this.plan.live_captions?.complete ? this.ccBtn : null,
        this.gearBtn, pipBtn, theaterBtn, this.fsBtn));
    this.spinner = h("div", { class: "spinner" });
    this.settings = h("div", { class: "settings", hidden: true });
    this.root.append(this.video, this.poster || "", this.titleBar, this.captionBox, this.spinner, this.flash, this.hintL, this.hintR,
      this.bigPlay, this.controls, this.settings);
    this.wire();
  }

  wire() {
    const v = this.video;
    v.volume = +this.vol.value;
    v.addEventListener("play", () => { this.root.classList.remove("paused"); this.playBtn.replaceChildren(icon("pause")); this.bigPlay.hidden = true; this.poster?.remove(); this.opts.onPlay?.(); });
    v.addEventListener("pause", () => { this.root.classList.add("paused"); this.playBtn.replaceChildren(icon("play")); this.saveProgress(true); });
    v.addEventListener("waiting", () => this.root.classList.add("buffering"));
    v.addEventListener("playing", () => this.root.classList.remove("buffering"));
    v.addEventListener("canplay", () => this.root.classList.remove("buffering"));
    v.addEventListener("timeupdate", () => this.tick());
    v.addEventListener("progress", () => this.drawBuffer());
    v.addEventListener("volumechange", () => this.muteBtn.replaceChildren(icon(v.muted || v.volume === 0 ? "mute" : "volume")));
    v.addEventListener("ended", () => { this.saveProgress(true, true); this.opts.onEnded?.(); });
    v.addEventListener("error", () => this.onMediaError());
    v.addEventListener("click", () => this.toggle());
    v.addEventListener("dblclick", () => this.fullscreen());

    // idle hiding
    let idleTimer;
    const wake = () => { this.root.classList.remove("idle"); clearTimeout(idleTimer); idleTimer = setTimeout(() => !v.paused && this.root.classList.add("idle"), 2600); };
    this.root.addEventListener("mousemove", wake);
    this.root.addEventListener("touchstart", wake, { passive: true });

    // double-tap seek on touch
    let lastTap = 0;
    v.addEventListener("touchend", (e) => {
      const now = Date.now();
      const x = e.changedTouches[0].clientX - this.root.getBoundingClientRect().left;
      const third = this.root.clientWidth / 3;
      if (now - lastTap < 300 && (x < third || x > 2 * third)) {
        e.preventDefault();
        this.seekBy(x < third ? -10 : 10);
      }
      lastTap = now;
    });

    // seek bar
    const posFromEvent = (e) => {
      const r = this.bar.getBoundingClientRect();
      return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    };
    this.bar.addEventListener("pointermove", (e) => this.showPreview(posFromEvent(e)));
    this.bar.addEventListener("pointerdown", (e) => {
      this.bar.setPointerCapture(e.pointerId);
      this.bar.classList.add("drag");
      this.dragging = true;
      const move = (ev) => { const p = posFromEvent(ev); this.played.style.width = `${p * 100}%`; this.knob.style.left = `${p * 100}%`; this.showPreview(p); };
      const up = (ev) => {
        this.bar.classList.remove("drag");
        this.dragging = false;
        this.bar.removeEventListener("pointermove", move);
        this.seek(posFromEvent(ev) * this.duration());
      };
      this.bar.addEventListener("pointermove", move);
      this.bar.addEventListener("pointerup", up, { once: true });
      move(e);
    });

    this.keyHandler = (e) => {
      if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      const map = {
        " ": () => this.toggle(), k: () => this.toggle(), j: () => this.seekBy(-10), l: () => this.seekBy(10),
        arrowleft: () => this.seekBy(-5), arrowright: () => this.seekBy(5), f: () => this.fullscreen(), m: () => { v.muted = !v.muted; },
        c: () => this.toggleCaptions(), i: () => this.pip(), t: () => this.opts.onTheater?.(),
        arrowup: () => { v.volume = Math.min(1, v.volume + 0.05); this.vol.value = v.volume; },
        arrowdown: () => { v.volume = Math.max(0, v.volume - 0.05); this.vol.value = v.volume; },
        ">": () => this.setSpeed(SPEEDS[Math.min(SPEEDS.length - 1, SPEEDS.indexOf(v.playbackRate) + 1)]),
        "<": () => this.setSpeed(SPEEDS[Math.max(0, SPEEDS.indexOf(v.playbackRate) - 1)]),
        home: () => this.seek(0), end: () => this.seek(this.duration() - 1),
      };
      if (k === "n" && e.shiftKey) { this.opts.onNext?.(); return; }
      if (/^[0-9]$/.test(k)) { this.seek((+k / 10) * this.duration()); e.preventDefault(); return; }
      if (map[k]) { e.preventDefault(); map[k](); wake(); }
    };
    document.addEventListener("keydown", this.keyHandler);
    this.fsHandler = () => {
      const fs = document.fullscreenElement === this.root;
      this.root.classList.toggle("fs", fs);
      this.fsBtn?.replaceChildren(icon(fs ? "exitFullscreen" : "fullscreen"));
    };
    document.addEventListener("fullscreenchange", this.fsHandler);
    this.docClick = (e) => { if (!this.settings.hidden && !this.settings.contains(e.target)) this.settings.hidden = true; };
    document.addEventListener("click", this.docClick);
    this.unload = () => this.saveProgress(true);
    window.addEventListener("pagehide", this.unload);
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: this.item.title, artwork: this.item.has_thumb ? [{ src: `/api/items/${this.item.id}/thumb`, sizes: "640x360", type: "image/webp" }] : [] });
      navigator.mediaSession.setActionHandler("seekbackward", () => this.seekBy(-10));
      navigator.mediaSession.setActionHandler("seekforward", () => this.seekBy(10));
    }
  }

  // ---------- loading & quality ----------
  load(qualityId, at = 0, initial = false) {
    const q = this.plan.qualities.find((x) => x.id === qualityId) || this.plan.qualities[0];
    if (!q) { this.showError(this.plan.reason || "This item can't be played."); return; }
    const wasPaused = initial ? true : this.video.paused;
    const rate = this.video.playbackRate || 1;
    this.quality = q.id;
    this.destroyHls();
    this.offset = 0;
    this.progressive = false;
    this.root.querySelector(".error")?.remove();
    const v = this.video;
    // keep subtitle <track>s across source swaps
    v.querySelectorAll("track").forEach((t) => t.remove());
    if (q.type === "direct") {
      v.src = q.url;
    } else if (q.type === "hls" && window.Hls?.isSupported() && !(nativeHls() && /iPhone|iPad|Macintosh/.test(navigator.userAgent) && !window.MediaSource)) {
      this.hls = new window.Hls({
        startPosition: at, maxBufferLength: 60, maxMaxBufferLength: 180, backBufferLength: 90,
        capLevelToPlayerSize: q.id === "auto", startLevel: -1, abrEwmaDefaultEstimate: 8_000_000, fragLoadingTimeOut: 90000,
        xhrSetup: (xhr) => { xhr.withCredentials = true; },
      });
      this.hls.on(window.Hls.Events.LEVEL_SWITCHED, () => this.updateQualityTag());
      this.hls.on(window.Hls.Events.ERROR, (_, d) => {
        if (!d.fatal) return;
        if (d.type === window.Hls.ErrorTypes.NETWORK_ERROR && (this.netRetries = (this.netRetries || 0) + 1) < 4) { this.hls.startLoad(); return; }
        if (d.type === window.Hls.ErrorTypes.MEDIA_ERROR && !this.recovered) { this.recovered = true; this.hls.recoverMediaError(); return; }
        this.onMediaError();
      });
      this.hls.loadSource(q.url);
      this.hls.attachMedia(v);
    } else if (q.type === "hls" && nativeHls()) {
      v.src = q.url;
    } else if (q.progressive) {
      this.progressive = true;
      this.offset = at;
      v.src = `${q.progressive}&start=${at.toFixed(2)}`;
      at = 0;
    } else if (q.type === "progressive") {
      this.progressive = true;
      this.offset = at;
      v.src = `${q.url}?start=${at.toFixed(2)}`;
      at = 0;
    } else {
      v.src = q.url;
    }
    this.addTracks();
    const start = () => {
      if (at && !this.hls) { try { v.currentTime = at; } catch { /* not seekable yet */ } }
      v.playbackRate = rate;
      if (!wasPaused || (initial && this.opts.autoplay)) v.play().catch(() => { /* autoplay blocked: show big play */ });
    };
    v.addEventListener("loadedmetadata", start, { once: true });
    this.updateQualityTag();
  }

  setQuality(id) {
    localStorage.setItem("quality", id);
    this.load(id, this.currentTime());
  }

  destroyHls() { if (this.hls) { this.hls.destroy(); this.hls = null; } }

  updateQualityTag() {
    const lvl = this.hls && this.hls.currentLevel >= 0 ? this.hls.levels[this.hls.currentLevel] : null;
    let tag = "";
    if (this.quality === "original") tag = "SRC";
    else if (this.quality === "auto" && lvl) tag = lvl.height >= 2000 ? "4K" : lvl.height >= 1000 ? "HD" : "";
    else if (/^(2160|1440|1080|720)p$/.test(this.quality)) tag = this.quality === "2160p" ? "4K" : "HD";
    this.qTag.textContent = tag;
    this.qTag.hidden = !tag;
  }

  qualityLabel(q) {
    if (q.id === "auto") {
      const lvl = this.hls && this.hls.currentLevel >= 0 ? this.hls.levels[this.hls.currentLevel] : null;
      return lvl ? `Auto (${lvl.height ? lvl.height + "p" : "source"})` : "Auto";
    }
    return q.label;
  }

  onMediaError() {
    // Direct play failed (e.g. codec the browser lied about): fall back to the transcoded ladder once.
    if (this.quality !== "auto" && !this.fellBack && this.plan.qualities.some((q) => q.id === "auto")) {
      this.fellBack = true;
      toast("Switching to a compatible stream…");
      this.load("auto", this.currentTime());
      return;
    }
    this.showError("Playback failed. The file may be damaged or the transcoder unavailable.");
  }

  showError(msg) {
    this.root.querySelector(".error")?.remove();
    this.root.append(h("div", { class: "error" }, h("div", null, icon("warning"), h("p", null, msg))));
  }

  // ---------- time ----------
  duration() { return this.plan.duration || (isFinite(this.video.duration) ? this.video.duration : 0); }
  currentTime() { return this.offset + (this.video.currentTime || 0); }

  seek(t) {
    t = Math.max(0, Math.min(t, this.duration() - 0.2));
    if (this.progressive) { this.load(this.quality, t); return; }
    this.video.currentTime = t;
  }
  seekBy(d) {
    this.seek(this.currentTime() + d);
    const hint = d < 0 ? this.hintL : this.hintR;
    hint.classList.remove("show"); void hint.offsetWidth; hint.classList.add("show");
  }
  toggle() {
    if (this.video.paused) this.video.play().catch(() => {});
    else this.video.pause();
    this.flash.replaceChildren(icon(this.video.paused ? "pause" : "play"));
    this.flash.classList.remove("show"); void this.flash.offsetWidth; this.flash.classList.add("show");
  }

  tick() {
    const t = this.currentTime(), d = this.duration();
    if (!this.dragging && d) {
      this.played.style.width = `${(100 * t) / d}%`;
      this.knob.style.left = `${(100 * t) / d}%`;
    }
    this.time.textContent = `${fmtDuration(t)} / ${fmtDuration(d)}`;
    this.renderCaptions(t);
    if (Date.now() - this.lastSaved > 10000) this.saveProgress();
    if (this.captionMode === "live" && Date.now() - (this.lastLive || 0) > 2500) this.fetchLive();
    this.opts.onTime?.(t);
  }

  drawBuffer() {
    const v = this.video, d = this.duration();
    if (!d || !v.buffered.length) return;
    let end = 0;
    for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= v.currentTime + 0.5) end = Math.max(end, v.buffered.end(i));
    this.buf.style.width = `${(100 * (this.offset + end)) / d}%`;
  }

  showPreview(p) {
    const d = this.duration();
    const t = p * d;
    this.hoverFill.style.width = `${p * 100}%`;
    this.prevTime.textContent = fmtDuration(t);
    const tp = this.plan.trickplay;
    const r = this.bar.getBoundingClientRect();
    const half = tp ? tp.width / 2 + 4 : 30;
    this.preview.style.left = `${Math.min(r.width - half, Math.max(half, p * r.width))}px`;
    if (tp) {
      const per = tp.cols * tp.rows;
      const idx = Math.min(Math.floor(t / tp.interval), tp.count - 1);
      const sheet = tp.sprites[Math.floor(idx / per)];
      const within = idx % per;
      Object.assign(this.prevFrame.style, {
        width: `${tp.width}px`, height: `${tp.height}px`,
        backgroundImage: sheet ? `url("${tp.base}${sheet}")` : "none",
        backgroundPosition: `-${(within % tp.cols) * tp.width}px -${Math.floor(within / tp.cols) * tp.height}px`,
      });
    }
  }

  saveProgress(force = false, completed = false) {
    const t = this.currentTime();
    if (!force && t < 5) return;
    this.lastSaved = Date.now();
    const body = JSON.stringify({ position: completed ? this.duration() : t, duration: this.duration() || null });
    if (force && navigator.sendBeacon && document.visibilityState === "hidden") {
      fetch(`/api/items/${this.item.id}/progress`, { method: "POST", body, keepalive: true, headers: { "content-type": "application/json", "x-paddock": "1" } }).catch(() => {});
      return;
    }
    fetch(`/api/items/${this.item.id}/progress`, { method: "POST", body, headers: { "content-type": "application/json", "x-paddock": "1" } }).catch(() => {});
  }

  // ---------- captions ----------
  addTracks() {
    this.plan.subtitles.forEach((s, i) => {
      const tr = h("track", { kind: "subtitles", label: s.label, src: s.url, srclang: (s.label || "en").slice(0, 2) });
      this.video.append(tr);
      tr.track.mode = "hidden";
      tr.addEventListener("cuechange", () => { if (this.captionMode === `track:${i}`) this.renderTrackCue(tr.track); });
    });
    this.applyCaptionMode();
  }

  captionOptions() {
    const opts = [["off", "Off"]];
    this.plan.subtitles.forEach((s, i) => opts.push([`track:${i}`, s.label]));
    if (this.plan.live_captions?.complete) opts.push(["live", "Transcript (AI)"]);
    else if (this.plan.live_captions?.enabled) opts.push(["live", "Live transcription (AI)"]);
    return opts;
  }

  toggleCaptions() {
    const opts = this.captionOptions();
    if (opts.length < 2) { toast("No captions for this video. An admin can enable live transcription in Manage server → Transcription."); return; }
    this.setCaptions(this.captionMode === "off" ? (localStorage.getItem("captions-last") || opts[1][0]) : "off");
  }

  setCaptions(mode) {
    if (!this.captionOptions().some(([v]) => v === mode)) mode = "off";
    this.captionMode = mode;
    localStorage.setItem("captions", mode);
    if (mode !== "off") localStorage.setItem("captions-last", mode);
    this.applyCaptionMode();
  }

  applyCaptionMode() {
    this.ccBtn.classList.toggle("cc-on", this.captionMode !== "off");
    this.captionBox.replaceChildren();
    if (this.captionMode === "live") { this.lastLive = 0; this.fetchLive(); }
  }

  async fetchLive() {
    this.lastLive = Date.now();
    try {
      const r = await api.get(`/api/items/${this.item.id}/captions/live?at=${this.currentTime().toFixed(1)}`, { quiet: true });
      const known = new Set(this.liveCues.map((c) => c.start));
      for (const c of r.cues) if (!known.has(c.start)) this.liveCues.push(c);
      this.liveCues.sort((a, b) => a.start - b.start);
      this.liveStatus = r.complete ? "" : r.pending ? "Transcribing…" : "";
      this.opts.onCues?.(this.liveCues, r);
    } catch { /* ignore */ }
  }

  renderCaptions(t) {
    if (this.captionMode !== "live") return;
    const active = this.liveCues.filter((c) => c.start <= t && c.end >= t);
    const text = active.map((c) => c.text).join("\n");
    if (text === this.lastCaption && !this.liveStatus === !this.lastStatus) return;
    this.lastCaption = text;
    this.lastStatus = this.liveStatus;
    this.captionBox.replaceChildren(...(this.liveStatus && !text ? [h("div", { class: "live" }, this.liveStatus)] : []),
      ...text.split("\n").filter(Boolean).map((line) => h("div", null, h("span", null, line))));
  }

  renderTrackCue(track) {
    const cues = [...(track.activeCues || [])].map((c) => c.text.replace(/<[^>]+>/g, ""));
    this.captionBox.replaceChildren(...cues.map((line) => h("div", null, h("span", null, line))));
  }

  // ---------- settings menu ----------
  toggleSettings() {
    if (!this.settings.hidden) { this.settings.hidden = true; return; }
    this.settingsMain();
    this.settings.hidden = false;
  }

  settingsMain() {
    const v = this.video;
    const q = this.plan.qualities.find((x) => x.id === this.quality);
    const capLabel = this.captionOptions().find(([m]) => m === this.captionMode)?.[1] || "Off";
    this.settings.replaceChildren(
      this.plan.qualities.length > 1 ? h("button", { onclick: (e) => { e.stopPropagation(); this.settingsQuality(); } }, icon("hd", "sm"), "Quality", h("span", { class: "val" }, q ? this.qualityLabel(q) : "")) : null,
      h("button", { onclick: (e) => { e.stopPropagation(); this.settingsSpeed(); } }, icon("speed", "sm"), "Playback speed", h("span", { class: "val" }, v.playbackRate === 1 ? "Normal" : `${v.playbackRate}×`)),
      this.captionOptions().length > 1 ? h("button", { onclick: (e) => { e.stopPropagation(); this.settingsCaptions(); } }, icon("cc", "sm"), "Subtitles/CC", h("span", { class: "val" }, capLabel)) : null,
      h("button", { onclick: (e) => { e.stopPropagation(); this.toggleStats(); this.settings.hidden = true; } }, icon("info", "sm"), "Stats for nerds"),
    );
  }

  submenu(title, options, current, pick) {
    this.settings.replaceChildren(
      h("button", { class: "back", onclick: (e) => { e.stopPropagation(); this.settingsMain(); } }, icon("chevronLeft", "sm"), title),
      ...options.map(([val, label, hint]) => h("button", { onclick: (e) => { e.stopPropagation(); pick(val); this.settings.hidden = true; } },
        h("span", { class: "check" }, val === current ? icon("check", "sm") : ""), label, hint ? h("span", { class: "val" }, hint) : null)));
  }

  settingsQuality() {
    const opts = this.plan.qualities.map((q) => [q.id, this.qualityLabel(q),
      q.id === "original" ? (q.type === "direct" ? "full quality" : "full quality, remux") : q.id === "auto" ? "adapts to your network" : ""]);
    this.submenu("Quality", opts, this.quality, (id) => this.setQuality(id));
  }

  settingsSpeed() {
    this.submenu("Playback speed", SPEEDS.map((s) => [s, s === 1 ? "Normal" : `${s}`]), this.video.playbackRate, (s) => this.setSpeed(s));
  }

  setSpeed(s) { if (s) { this.video.playbackRate = s; toast(`${s}×`, { ms: 900 }); } }

  settingsCaptions() {
    this.submenu("Subtitles/CC", this.captionOptions(), this.captionMode, (m) => this.setCaptions(m));
  }

  toggleStats() {
    if (this.statsEl) { clearInterval(this.statsTimer); this.statsEl.remove(); this.statsEl = null; return; }
    this.statsEl = h("div", { class: "stats" });
    this.root.append(this.statsEl);
    const upd = () => {
      const v = this.video;
      const lvl = this.hls && this.hls.currentLevel >= 0 ? this.hls.levels[this.hls.currentLevel] : null;
      const q = v.getVideoPlaybackQuality?.();
      const bufAhead = v.buffered.length ? (v.buffered.end(v.buffered.length - 1) - v.currentTime).toFixed(1) : 0;
      this.statsEl.textContent = [
        `Item        #${this.item.id} ${this.item.ext || ""}`,
        `Mode        ${this.plan.mode}${this.progressive ? " (progressive)" : ""} · ${this.quality}`,
        `Viewport    ${v.clientWidth}×${v.clientHeight} · video ${v.videoWidth}×${v.videoHeight}`,
        lvl ? `Level       ${lvl.width}×${lvl.height} @ ${(lvl.bitrate / 1e6).toFixed(1)} Mbps` : null,
        this.hls ? `Bandwidth   ${(this.hls.bandwidthEstimate / 1e6).toFixed(1)} Mbps est.` : null,
        `Buffer      ${bufAhead}s ahead`,
        q ? `Frames      ${q.droppedVideoFrames} dropped / ${q.totalVideoFrames}` : null,
        `Server GPU  ${this.plan.hwaccel}`,
      ].filter(Boolean).join("\n");
    };
    upd();
    this.statsTimer = setInterval(upd, 1000);
  }

  // ---------- modes ----------
  async fullscreen() {
    if (document.fullscreenElement) await document.exitFullscreen();
    else {
      await this.root.requestFullscreen?.().catch(() => {});
      try { await screen.orientation?.lock?.("landscape"); } catch { /* not supported */ }
    }
  }
  async pip() {
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await this.video.requestPictureInPicture();
    } catch { toast("Picture-in-picture isn't available here"); }
  }

  destroy() {
    this.saveProgress(true);
    this.destroyHls();
    this.video.removeAttribute("src");
    this.video.load();
    clearInterval(this.statsTimer);
    document.removeEventListener("keydown", this.keyHandler);
    document.removeEventListener("fullscreenchange", this.fsHandler);
    document.removeEventListener("click", this.docClick);
    window.removeEventListener("pagehide", this.unload);
  }
}
