// Live things that follow you around the app: one events connection (calls ringing, watch parties starting,
// new comments) and voice chat, which stays on while you browse. The dock in the corner shows who's talking.

import { api } from "./api.js";
import { ctx, navigate } from "./app.js";
import { h, icon, iconBtn, toast } from "./ui.js";
import { VoiceSession, voiceSupported } from "./voice.js";

// ---- voice, one room at a time, kept across pages -------------------------------------------------------

class VoiceHub extends EventTarget {
  constructor() {
    super();
    this.session = null;
    this.room = null;
    this.title = "";
    this.speaking = new Set();
    this.level = 0;
  }

  get members() { return this.session?.members || []; }
  get me() { return this.session?.me ?? null; }
  get muted() { return !!this.session?.muted; }
  get deafened() { return !!this.session?.deafened; }
  othersTalking() { return [...this.speaking].some((id) => id !== this.me); }
  emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }

  async join(room, title) {
    if (this.room === room && this.session && !this.session.closed) return;
    if (!voiceSupported()) { toast("Voice needs a browser with microphone support", { error: true }); return; }
    this.leave(true);
    const s = new VoiceSession(room);
    this.session = s; this.room = room; this.title = title; this.speaking = new Set();
    s.addEventListener("roster", () => this.emit("change"));
    s.addEventListener("speaking", (e) => {
      if (e.detail.value) this.speaking.add(e.detail.id); else this.speaking.delete(e.detail.id);
      this.emit("speaking", e.detail); this.emit("change");
    });
    s.addEventListener("level", (e) => { this.level = e.detail; this.emit("level", e.detail); });
    s.addEventListener("error", (e) => { toast(e.detail, { error: true }); if (s.closed && this.session === s) this.leave(); });
    this.emit("change");
    try { await s.start(); } catch (e) { toast(e.message, { error: true }); if (this.session === s) this.leave(); }
    this.emit("change");
  }

  leave(quiet = false) {
    this.session?.stop();
    this.session = null; this.room = null; this.title = ""; this.speaking = new Set();
    if (!quiet) this.emit("change");
  }

  setMuted(v) { this.session?.setMuted(v); this.emit("change"); }
  setDeafened(v) { this.session?.setDeafened(v); this.emit("change"); }
}

export const voiceHub = new VoiceHub();

function dock() {
  const el = h("aside", { class: "voice-dock", hidden: true, "aria-label": "Voice chat" });
  const draw = () => {
    if (!voiceHub.room) { el.hidden = true; document.body.classList.remove("in-voice"); return; }
    el.hidden = false;
    document.body.classList.add("in-voice");
    const level = h("div", { class: "meter voice-level" }, h("i", { style: { width: `${Math.round(voiceHub.level * 100)}%` } }));
    voiceHub.onlevel = level.firstChild;
    el.replaceChildren(
      h("div", { class: "row" }, h("span", { class: "live-dot" }),
        h("a", { href: "/together", class: "voice-title" }, voiceHub.title || "Voice chat"),
        h("span", { class: "muted small" }, `${voiceHub.members.length} here`)),
      h("div", { class: "voice-people" }, ...voiceHub.members.map((m) => h("span", {
        class: `avatar sm${voiceHub.speaking.has(m.id) && !m.muted ? " talking" : ""}${m.muted ? " muted" : ""}`,
        title: `${m.username}${m.id === voiceHub.me ? " (you)" : ""}${m.muted ? " · muted" : ""}`,
      }, m.username[0].toUpperCase()))),
      level,
      h("div", { class: "row" },
        iconBtn(voiceHub.muted ? "mute" : "volume", voiceHub.muted ? "Unmute" : "Mute", () => voiceHub.setMuted(!voiceHub.muted),
          { class: voiceHub.muted ? "danger" : "" }),
        h("button", { class: `btn sm${voiceHub.deafened ? " danger" : " ghost"}`, onclick: () => voiceHub.setDeafened(!voiceHub.deafened) },
          voiceHub.deafened ? "Undeafen" : "Deafen"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn sm danger", onclick: () => voiceHub.leave() }, "Leave")));
  };
  voiceHub.addEventListener("change", draw);
  voiceHub.addEventListener("level", (e) => { if (voiceHub.onlevel) voiceHub.onlevel.style.width = `${Math.round(e.detail * 100)}%`; });
  return el;
}

// ---- events ------------------------------------------------------------------------------------------

let ws = null, retry = null, pinger = null;
let ringing = null;

export function startLive() {
  if (!document.querySelector(".voice-dock")) document.body.append(dock());
  connect();
}

export function stopLive() {
  clearTimeout(retry); clearInterval(pinger);
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  voiceHub.leave();
}

function connect() {
  clearTimeout(retry);
  if (!ctx.user) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/api/events/ws`);
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch { /* ignore bad events */ } };
  ws.onclose = () => { clearInterval(pinger); if (ctx.user) retry = setTimeout(connect, 3000 + Math.random() * 3000); };
  ws.onopen = () => { clearInterval(pinger); pinger = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000); };
}

function handle(msg) {
  window.dispatchEvent(new CustomEvent("paddock:event", { detail: msg }));
  switch (msg.type) {
    case "hello": for (const c of msg.ringing || []) ring(c); break;
    case "ring": ring(msg.call); break;
    case "ring_stop": case "call_ended":
      if (ringing?.call.id === msg.call_id) ringing.close();
      if (voiceHub.room === `call:${msg.call_id}`) { voiceHub.leave(); toast("The call ended"); }
      break;
    case "declined": toast(`${msg.name} can't talk right now`); break;
    case "party":
      toast(`${msg.party.host} started a watch party${msg.party.community ? ` in ${msg.party.community}` : ""}: ${msg.party.title}`,
        { action: { label: "Join", fn: () => navigate(`/party/${msg.party.id}`) }, ms: 15000 });
      break;
    case "comment":
      if (!location.pathname.match(new RegExp(`^/(watch|item)/${msg.comment.item_id}$`))) {
        toast(`${msg.comment.display_name} commented on ${msg.title}: “${msg.comment.body.slice(0, 80)}”`,
          { action: { label: "Open", fn: () => navigate(`/go/${msg.comment.item_id}`) } });
      }
      break;
  }
}

// ---- an incoming call ---------------------------------------------------------------------------------------

function ring(call) {
  if (!call.ringing || ringing?.call.id === call.id || voiceHub.room === call.room) return;
  ringing?.close();
  let tone = null;
  try {
    const ac = new AudioContext();
    const gain = ac.createGain(); gain.gain.value = 0; gain.connect(ac.destination);
    const osc = ac.createOscillator(); osc.frequency.value = 660; osc.connect(gain); osc.start();
    let on = false;
    const t = setInterval(() => { on = !on; gain.gain.setTargetAtTime(on ? 0.08 : 0, ac.currentTime, 0.02); }, 600);
    tone = () => { clearInterval(t); osc.stop(); ac.close(); };
  } catch { /* no audio until the page has been clicked */ }
  const others = call.people.filter((p) => p.user_id !== ctx.user.id && p.user_id !== call.host_id).map((p) => p.name);
  const el = h("div", { class: "ring-card", role: "alertdialog", "aria-label": `${call.host} is calling` },
    h("span", { class: "avatar lg talking" }, call.host[0].toUpperCase()),
    h("div", null, h("b", null, `${call.host} is calling`),
      h("div", { class: "muted small" }, others.length ? `with ${others.join(", ")}` : "Voice call")),
    h("div", { class: "row" },
      h("button", { class: "btn danger", onclick: async () => { close(); await api.post(`/api/calls/${call.id}/decline`).catch(() => {}); } }, "Decline"),
      h("button", { class: "btn accent", onclick: () => { close(); voiceHub.join(call.room, `Call with ${call.host}`); } }, icon("volume", "sm"), "Answer")));
  document.body.append(el);
  const stopAt = setTimeout(() => close(), 45000);
  function close() { clearTimeout(stopAt); tone?.(); el.remove(); if (ringing?.el === el) ringing = null; }
  ringing = { call, el, close };
}

/** Start a call with people (ids); joins its voice room right away. */
export async function callPeople(userIds, label) {
  const c = await api.post("/api/calls", { user_ids: userIds });
  await voiceHub.join(c.room, label || `Call with ${c.people.filter((p) => p.user_id !== ctx.user.id).map((p) => p.name).join(", ")}`);
  return c;
}
