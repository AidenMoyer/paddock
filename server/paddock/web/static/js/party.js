// Watch parties in the web app: keep this player in step with everyone else's, plus the party panel
// (members, invite, voice, chat, reactions). The server owns the clock (see paddock/party.py).

import { api } from "./api.js";
import { ctx, navigate } from "./app.js";
import { h, icon, iconBtn, toast, copyText, confirmDialog, modal, field, switchEl } from "./ui.js";
import { VoiceSession, voiceSupported } from "./voice.js";

export const REACTIONS = ["😂", "❤️", "😮", "👏", "😢", "🔥", "👀", "🎉"];

export class PartySync extends EventTarget {
  constructor(id, player) {
    super();
    this.id = id;
    this.player = player;
    this.video = player.video;
    this.offsets = [];
    this.state = null;
    this.suppressUntil = 0;
    this.closed = false;
    this.wire();
    this.connect();
    this.timer = setInterval(() => this.correct(), 500);
    this.pinger = setInterval(() => this.ping(), 10000);
  }

  // ---- connection ----
  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/api/parties/${this.id}/ws`);
    this.ws.onopen = () => { for (let i = 0; i < 4; i++) setTimeout(() => this.ping(), i * 250); };
    this.ws.onmessage = (e) => this.onMessage(JSON.parse(e.data));
    this.ws.onclose = (e) => {
      if (this.closed) return;
      if (e.code === 4404 || e.code === 4401) { this.emit("ended", { reason: "This watch party has ended or isn't shared with you." }); return; }
      this.emit("status", "Reconnecting…");
      setTimeout(() => !this.closed && this.connect(), 1500);
    };
  }

  send(msg) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(msg)); }
  emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
  ping() { this.send({ type: "ping", t0: Date.now() / 1000 }); }

  serverNow() {
    if (!this.offsets.length) return Date.now() / 1000;
    const sorted = [...this.offsets].sort((a, b) => a - b);
    return Date.now() / 1000 + sorted[Math.floor(sorted.length / 2)];
  }

  onMessage(msg) {
    switch (msg.type) {
      case "pong": {
        const t1 = Date.now() / 1000;
        this.offsets = [...this.offsets.slice(-6), msg.server - (msg.t0 + (t1 - msg.t0) / 2)];
        break;
      }
      case "hello":
        this.me = msg.you;
        this.party = msg.party;
        this.canControl = msg.can_control;
        this.offsets = [msg.server - Date.now() / 1000];
        this.emit("hello", msg);
        this.apply(msg.state, null, null);
        break;
      case "state": this.apply(msg.state, msg.by, msg.action); break;
      case "members": this.emit("members", msg.members); break;
      case "chat": case "notice": this.emit("chat", msg); break;
      case "reaction": this.emit("reaction", msg); break;
      case "error": toast(msg.error, { error: true }); break;
      case "ended": this.emit("ended", { reason: "The host ended the watch party." }); break;
    }
  }

  // ---- following the shared clock ----
  apply(state, by, action) {
    this.state = state;
    if (state.item_id !== this.player.item.id) { this.emit("switch", state); return; }
    if (by && by !== ctx.user.username && action) {
      const verb = { play: "pressed play", pause: "paused", seek: "skipped", item: "changed the video" }[action];
      if (verb) toast(`${by} ${verb}`, { ms: 1400 });
    }
    if (action === "hold") toast(`Waiting for ${state.waiting_for.join(", ")}…`, { ms: 1800 });
    this.correct(true);
    this.emit("state", state);
  }

  target() {
    const s = this.state;
    if (!s) return null;
    const now = this.serverNow();
    return s.playing && now > s.at ? s.position + (now - s.at) * s.rate : s.position;
  }

  quiet(ms = 800) { this.suppressUntil = performance.now() + ms; }

  correct(force = false) {
    const s = this.state;
    if (!s || s.item_id !== this.player.item.id || this.closed) return;
    const target = this.target();
    const cur = this.player.currentTime();
    const diff = cur - target;
    const startsIn = s.at - this.serverNow();
    if (s.playing) {
      if (this.video.paused) {
        if (startsIn > 0.05) {                      // a coordinated start is coming: line up, then go
          if (Math.abs(diff) > 0.3) { this.quiet(); this.player.seek(target); }
          clearTimeout(this.startTimer);
          this.startTimer = setTimeout(() => this.resume(), startsIn * 1000);
        } else {
          if (Math.abs(diff) > 0.5) { this.quiet(); this.player.seek(target); }
          this.resume();
        }
        return;
      }
      if (Math.abs(diff) > 1.0 || force && Math.abs(diff) > 0.5) {
        this.quiet(); this.player.seek(target); this.video.playbackRate = s.rate;
      } else if (Math.abs(diff) > 0.12) {
        this.video.playbackRate = s.rate * (diff > 0 ? 0.95 : 1.05);   // drift: catch up without a jump
      } else if (this.video.playbackRate !== s.rate) {
        this.video.playbackRate = s.rate;
      }
    } else {
      if (!this.video.paused) { this.quiet(); this.video.pause(); }
      if (Math.abs(diff) > 0.3) { this.quiet(); this.player.seek(target); }
    }
  }

  resume() {
    if (!this.video.paused) return;
    this.quiet();
    this.video.play().then(() => this.emit("gesture", false)).catch((e) => {
      if (e.name === "NotAllowedError") this.emit("gesture", true);   // browser wants a tap before playing sound
    });
  }

  // ---- what this person does with their player goes to everyone ----
  wire() {
    // Only deliberate actions count. Browsers pause video in hidden tabs and phones pause it when you switch
    // apps; that must never pause everyone else, so while hidden nothing is sent (we resync on return).
    const visible = () => document.visibilityState === "visible";
    const mine = () => performance.now() > this.suppressUntil && visible();
    this.onVisible = () => { if (visible()) { this.quiet(); this.correct(true); } };
    document.addEventListener("visibilitychange", this.onVisible);
    this.handlers = {
      play: () => { if (mine()) this.send({ type: "play", position: this.player.currentTime() }); },
      pause: () => { if (mine() && !this.video.ended) this.send({ type: "pause", position: this.player.currentTime() }); },
      seeked: () => { if (mine()) this.send({ type: "seek", position: this.player.currentTime() }); },
      waiting: () => { clearTimeout(this.bufTimer); if (visible()) this.bufTimer = setTimeout(() => { this.buffering = true; this.send({ type: "buffering", value: true }); }, 400); },
      playing: () => { clearTimeout(this.bufTimer); if (this.buffering) { this.buffering = false; this.send({ type: "buffering", value: false }); } },
      canplay: () => { clearTimeout(this.bufTimer); if (this.buffering) { this.buffering = false; this.send({ type: "buffering", value: false }); } },
    };
    for (const [ev, fn] of Object.entries(this.handlers)) this.video.addEventListener(ev, fn);
  }

  switchTo(item, play = true) { this.send({ type: "item", item_id: item.id, title: item.title, play }); }

  close() {
    this.closed = true;
    clearInterval(this.timer); clearInterval(this.pinger); clearTimeout(this.startTimer); clearTimeout(this.bufTimer);
    for (const [ev, fn] of Object.entries(this.handlers || {})) this.video.removeEventListener(ev, fn);
    document.removeEventListener("visibilitychange", this.onVisible);
    try { this.ws?.close(); } catch { /* closing */ }
  }
}

// ---- "Watch together" dialog ---------------------------------------------------------------------------

export async function startPartyDialog(item, player) {
  const people = await api.get("/api/people").catch(() => []);
  let everyone = true, control = "everyone", wait = true;
  const picked = new Set();
  const pick = h("div", { class: "people-pick", hidden: true }, people.filter((p) => p.username !== ctx.user.username).map((p) =>
    h("label", { class: "row small" }, h("input", { type: "checkbox", onchange: (e) => (e.target.checked ? picked.add(p.username) : picked.delete(p.username)) }), p.username)));
  const m = modal("Watch together", h("div", { class: "stack" },
    h("p", { class: "muted", style: { margin: 0 } }, "Everyone streams from this server in their own quality, and Paddock keeps all players in step: play, pause and skip happen for everyone."),
    switchEl("Anyone with an account here can join", true, (v) => { everyone = v; pick.hidden = v; }, "Turn off to invite specific people."),
    pick,
    switchEl("Everyone can control playback", true, (v) => { control = v ? "everyone" : "host"; }, "Off: only you can play, pause and skip."),
    switchEl("Wait for everyone when someone is buffering", true, (v) => { wait = v; })), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      const p = await api.post("/api/parties", { item_id: item.id, everyone, invite: [...picked], control, wait_for_all: wait,
        position: player ? player.currentTime() : 0 });
      m.close();
      copyText(`${location.origin}/party/${p.id}`);
      toast(`Watch party started · code ${p.id} · invite link copied`);
      navigate(`/watch/${item.id}?party=${p.id}`, { replace: true });
    } }, icon("users", "sm"), "Start the party")],
  });
}

// ---- the party panel on the watch page -------------------------------------------------------------------

export function partyPanel(sync, player) {
  let voice = null;
  let party = null;
  const roster = h("div", { class: "party-members" });
  const voiceBox = h("div", { class: "party-voice" });
  const chatList = h("div", { class: "party-chat" });
  const status = h("div", { class: "muted small" });
  const gesture = h("button", { class: "btn accent party-gesture", hidden: true, onclick: () => { sync.resume(); } }, icon("play", "sm"), "Tap to join playback");
  player.root.append(gesture);
  const floats = h("div", { class: "party-floats" });
  player.root.append(floats);
  let members = [];
  let speakingIds = new Set();

  const drawMembers = () => roster.replaceChildren(...members.map((m) => {
    const vms = voice ? voice.members.filter((x) => x.user_id === m.user_id) : [];   // one person may be on several devices
    const talking = vms.some((x) => speakingIds.has(x.id));
    return h("div", { class: `party-member${talking ? " talking" : ""}` },
      h("span", { class: "avatar" }, m.username[0].toUpperCase()),
      h("span", null, m.username, m.host ? h("span", { class: "muted small" }, " · host") : null),
      m.buffering ? h("span", { class: "pill warn" }, "buffering") : null,
      vms.length ? icon(vms.every((x) => x.muted) ? "mute" : "volume", "xs") : null);
  }));

  const drawVoice = () => {
    if (!voice) {
      voiceBox.replaceChildren(voiceSupported()
        ? h("button", { class: "btn sm", onclick: joinVoice }, icon("chat", "sm"), "Join voice")
        : h("span", { class: "muted small" }, "Voice needs a browser with microphone support."));
      return;
    }
    const level = h("div", { class: "meter voice-level" }, h("i"));
    voice.addEventListener("level", (e) => { level.firstChild.style.width = `${Math.round(e.detail * 100)}%`; });
    voiceBox.replaceChildren(
      h("button", { class: `btn sm${voice.muted ? " danger" : ""}`, onclick: () => { voice.setMuted(!voice.muted); drawVoice(); } },
        icon(voice.muted ? "mute" : "volume", "sm"), voice.muted ? "Unmute" : "Mute"),
      h("button", { class: `btn sm${voice.deafened ? " danger" : ""}`, onclick: () => { voice.setDeafened(!voice.deafened); drawVoice(); } },
        voice.deafened ? "Undeafen" : "Deafen"),
      h("button", { class: "btn sm ghost", onclick: leaveVoice }, "Leave voice"), level,
      h("div", { class: "muted small" }, "Tip: headphones stop the movie echoing into your mic."));
  };

  async function joinVoice() {
    voice = new VoiceSession(`party:${sync.id}`);
    voice.addEventListener("roster", drawMembers);
    voice.addEventListener("speaking", (e) => {
      if (e.detail.value) speakingIds.add(e.detail.id); else speakingIds.delete(e.detail.id);
      duck();
      drawMembers();
    });
    voice.addEventListener("error", (e) => { toast(e.detail, { error: true }); if (voice?.closed) { voice = null; drawVoice(); } });
    try { await voice.start(); } catch (e) { toast(e.message, { error: true }); voice = null; }
    drawVoice(); drawMembers();
  }
  function leaveVoice() { voice?.stop(); voice = null; speakingIds = new Set(); duck(); drawVoice(); drawMembers(); }

  // lower the movie a little while friends are talking
  const baseVolume = () => player.video.dataset.partyBase ? +player.video.dataset.partyBase : player.video.volume;
  function duck() {
    const others = [...speakingIds].some((id) => id !== voice?.me);
    if (others && !player.video.dataset.partyBase) { player.video.dataset.partyBase = player.video.volume; player.video.volume = baseVolume() * 0.45; }
    else if (!others && player.video.dataset.partyBase) { player.video.volume = baseVolume(); delete player.video.dataset.partyBase; }
  }

  const addChat = (msg) => {
    chatList.append(msg.type === "notice" ? h("div", { class: "muted small" }, msg.text)
      : h("div", { class: "chat-line" }, h("b", null, msg.user), " ", msg.text));
    chatList.scrollTop = chatList.scrollHeight;
  };
  const chatInput = h("input", { class: "input", placeholder: "Say something…", maxlength: 500, onkeydown: (e) => {
    if (e.key === "Enter" && chatInput.value.trim()) { sync.send({ type: "chat", text: chatInput.value.trim() }); chatInput.value = ""; }
    e.stopPropagation();   // typing mustn't trigger player shortcuts
  } });

  sync.addEventListener("hello", (e) => {
    party = e.detail.party;
    members = party.members;
    head.replaceChildren(h("div", { style: { flex: 1 } }, h("b", null, "Watch party"), h("div", { class: "muted small" },
      `Code ${party.id} · hosted by ${party.host}${party.control === "host" ? " · host controls" : ""}`)),
      iconBtn("link", "Copy invite link", () => { copyText(`${location.origin}/party/${party.id}`); toast("Invite link copied"); }),
      party.host_id === ctx.user.id ? h("button", { class: "btn sm danger", onclick: endParty }, "End") : h("button", { class: "btn sm ghost", onclick: leave }, "Leave"));
    chatList.replaceChildren();
    e.detail.chat.forEach(addChat);
    drawMembers();
  });
  sync.addEventListener("members", (e) => { members = e.detail; drawMembers(); });
  sync.addEventListener("chat", (e) => addChat(e.detail));
  sync.addEventListener("reaction", (e) => {
    const f = h("div", { class: "party-float", style: { left: `${10 + Math.random() * 75}%` } }, e.detail.emoji,
      h("span", { class: "who" }, e.detail.user));
    floats.append(f);
    setTimeout(() => f.remove(), 2600);
  });
  sync.addEventListener("state", (e) => { status.textContent = e.detail.hold ? `Waiting for ${e.detail.waiting_for.join(", ")}…` : ""; });
  sync.addEventListener("gesture", (e) => { gesture.hidden = !e.detail; });
  sync.addEventListener("status", (e) => { status.textContent = e.detail; });
  sync.addEventListener("ended", (e) => { toast(e.detail.reason); close(); navigate(location.pathname, { replace: true }); });

  async function endParty() {
    if (await confirmDialog("End the watch party?", "Everyone stays on this video but stops being synced.", { ok: "End party", danger: true })) {
      await api.del(`/api/parties/${sync.id}`);
    }
  }
  function leave() { close(); navigate(location.pathname, { replace: true }); }

  const head = h("div", { class: "row" }, h("b", null, "Watch party"), h("span", { class: "muted small" }, "connecting…"));
  const panel = h("div", { class: "party-panel" }, head, status, roster, voiceBox,
    h("div", { class: "party-reactions" }, REACTIONS.map((r) => h("button", { class: "reaction", onclick: () => sync.send({ type: "reaction", emoji: r }) }, r))),
    chatList, chatInput);
  drawVoice();

  function close() { voice?.stop(); voice = null; sync.close(); gesture.remove(); floats.remove(); }
  return { el: panel, close };
}
