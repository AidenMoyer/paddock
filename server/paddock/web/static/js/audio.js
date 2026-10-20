// Persistent music mini-player that survives page navigation, with Media Session (lock screen) controls.

import { api, thumbUrl } from "./api.js";
import { h, icon, iconBtn, fmtDuration } from "./ui.js";

let audio, bar, queue = [], index = 0, els = {};

function ensure() {
  if (audio) return;
  audio = new Audio();
  audio.preload = "auto";
  els.img = h("img", { alt: "" });
  els.title = h("div", { style: { fontWeight: 500 } });
  els.sub = h("div", { class: "muted small" });
  els.play = iconBtn("pause", "Play/pause", () => (audio.paused ? audio.play() : audio.pause()));
  els.fill = h("i");
  els.time = h("span", { class: "muted small hide-mobile" });
  const prog = h("div", { class: "prog" }, els.fill);
  prog.addEventListener("click", (e) => {
    const r = prog.getBoundingClientRect();
    if (audio.duration) audio.currentTime = ((e.clientX - r.left) / r.width) * audio.duration;
  });
  bar = h("div", { class: "miniplayer", role: "region", "aria-label": "Now playing" }, prog, els.img,
    h("div", { class: "t" }, els.title, els.sub),
    els.time,
    iconBtn("prev", "Previous", () => go(index - 1)), els.play, iconBtn("next", "Next", () => go(index + 1)),
    iconBtn("close", "Close player", stop));
  audio.addEventListener("play", () => els.play.replaceChildren(icon("pause")));
  audio.addEventListener("pause", () => els.play.replaceChildren(icon("play")));
  audio.addEventListener("ended", () => go(index + 1));
  audio.addEventListener("timeupdate", () => {
    if (audio.duration) els.fill.style.width = `${(100 * audio.currentTime) / audio.duration}%`;
    els.time.textContent = `${fmtDuration(audio.currentTime)} / ${fmtDuration(audio.duration || queue[index]?.duration)}`;
  });
  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("previoustrack", () => go(index - 1));
    navigator.mediaSession.setActionHandler("nexttrack", () => go(index + 1));
  }
}

async function go(i) {
  if (i < 0 || i >= queue.length) { if (i >= queue.length) audio.pause(); return; }
  index = i;
  const t = queue[i];
  const plan = await api.get(`/api/items/${t.id}/playback`);
  audio.src = plan.mode === "direct" ? plan.url : plan.progressive;
  audio.play().catch(() => {});
  els.title.textContent = t.title;
  els.sub.textContent = [t.artist, t.album].filter(Boolean).join(" · ");
  els.img.src = t.has_thumb ? thumbUrl(t.id) : "/static/icon.svg";
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: t.title, artist: t.artist || "", album: t.album || "",
      artwork: t.has_thumb ? [{ src: thumbUrl(t.id), sizes: "512x512", type: "image/webp" }] : [],
    });
  }
}

export function playQueue(tracks, start = 0) {
  ensure();
  queue = tracks;
  if (!bar.isConnected) { document.body.append(bar); document.body.classList.add("has-mini"); }
  go(start);
}

export function stop() {
  audio?.pause();
  bar?.remove();
  document.body.classList.remove("has-mini");
}

export function pauseMusic() { audio?.pause(); }
