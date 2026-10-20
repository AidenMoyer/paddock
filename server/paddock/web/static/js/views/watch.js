// Watch page: player + details + up next + transcript.

import { api, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle, itemHref } from "../app.js";
import { pauseMusic } from "../audio.js";
import { addToProjectDialog, addToCollectionDialog, itemCard, thumbBox } from "../cards.js";
import { queue } from "../queue.js";
import { Player, clientCodecs } from "../player.js";
import { PartySync, partyPanel, startPartyDialog } from "../party.js";
import { h, icon, iconBtn, fmtDuration, fmtBytes, fmtDateTime, timeAgo, toast, menu, promptDialog, resLabel, copyText, LIB_ICON } from "../ui.js";

export async function render(root, { match, query }) {
  const id = +match[1];
  const partyId = query?.get("party")?.toUpperCase() || null;
  document.body.classList.add("watching");
  pauseMusic();
  const codecs = clientCodecs().join(",");
  const [item, plan, upNext] = await Promise.all([api.get(`/api/items/${id}`), api.get(`/api/items/${id}/playback?codecs=${codecs}`),
    api.get(`/api/items/${id}/up_next`, { quiet: true }).catch(() => [])]);
  queue.started(id);
  setTitle(item.title);
  const theater = localStorage.getItem("theater") === "1";
  const layout = h("div", { class: `watch ${theater ? "theater" : ""}` });
  const primary = h("div", { class: "primary" });
  const secondary = h("div", { class: "secondary" });
  layout.append(primary, secondary);
  root.append(layout);

  // what plays next: your queue first, then Up next
  const nextItem = () => queue.next() || upNext[0] || null;

  let player = null;
  const playerWrap = h("div", { class: "player-wrap" });
  if (plan.mode === "unavailable") {
    playerWrap.append(h("div", { class: "player" }, item.has_thumb ? h("img", { class: "poster-img", src: thumbUrl(id), alt: "" }) : null,
      h("div", { class: "error" }, h("div", null, icon("warning"), h("p", null, plan.reason),
        item.kind === "red_clip" && ctx.user.is_admin ? h("button", { class: "btn accent", onclick: async () => {
          await api.post(`/api/items/${id}/proxy`); toast("Rendering a proxy with REDline — check background jobs");
        } }, "Make proxy") : null))));
  } else {
    player = new Player(item, plan, {
      autoplay: !partyId,                 // in a watch party the shared clock decides when to play
      onTheater: () => { layout.classList.toggle("theater"); localStorage.setItem("theater", layout.classList.contains("theater") ? "1" : "0"); },
      onEnded: () => { const n = nextItem(); if (n && localStorage.getItem("autoplay") !== "0") setTimeout(() => goNext(n), 1500); },
      onNext: () => { const n = nextItem(); if (n) goNext(n); },
      onCues: (cues) => drawTranscript(cues),
      onTime: (t) => highlightTranscript(t),
    });
    playerWrap.append(player.root);
    setTimeout(() => player.root.focus({ preventScroll: true }), 50);
  }
  primary.append(playerWrap);

  // ---- watch party: follow the shared clock, show the party panel
  let sync = null, party = null;
  if (partyId && player) {
    sync = new PartySync(partyId, player);
    party = partyPanel(sync, player);
    secondary.append(party.el);
    sync.addEventListener("switch", (e) => navigate(`/watch/${e.detail.item_id}?party=${partyId}`, { replace: true }));
  }
  function goNext(n) {
    if (sync) { if (sync.canControl) sync.switchTo(n); return; }   // the party moves on together
    navigate(`/watch/${n.id}`);
  }

  // ---- title + actions
  primary.append(h("h1", { class: "watch-title" }, item.title));
  const favBtn = h("button", { class: "btn", onclick: async () => {
    const r = await api.patch(`/api/items/${id}`, { favorite: !item.favorite });
    item.favorite = r.favorite;
    favBtn.replaceChildren(icon(item.favorite ? "heart" : "heartOutline"), item.favorite ? "Favorited" : "Favorite");
  } }, icon(item.favorite ? "heart" : "heartOutline"), item.favorite ? "Favorited" : "Favorite");
  const moreBtn = iconBtn("more", "More", (e) => {
    e.stopPropagation();
    menu(moreBtn, [
      { icon: "queue", label: "Add to collection", fn: () => addToCollectionDialog([id]) },
      { icon: "tag", label: "Add tag", fn: addTag },
      plan.live_captions?.enabled ? { icon: "cc", label: "Transcribe whole video", fn: async () => { await api.post(`/api/items/${id}/transcribe`); toast("Transcription queued"); } } : null,
      { icon: "sparkle", label: "AI describe & tag", fn: async () => { await api.post("/api/ai/autotag", { item_ids: [id] }); toast("AI tagging started"); } },
      ctx.user.is_admin && plan.mode !== "direct" ? { icon: "bolt", label: "Optimize for phones (pre-transcode)", fn: async () => { await api.post(`/api/items/${id}/optimize`); toast("Optimizing in the background"); } } : null,
      { icon: "restore", label: "Mark as unwatched", fn: async () => { await api.del(`/api/items/${id}/progress`); toast("Marked unwatched"); } },
      "-",
      { icon: "trash", label: "Move to trash", fn: async () => {
        await api.post("/api/items/trash", { ids: [id] });
        toast("Moved to trash", { action: { label: "Undo", fn: () => api.post("/api/items/restore", { ids: [id] }) } });
        history.back();
      } },
    ]);
  });
  primary.append(h("div", { class: "watch-actions" },
    h("div", { class: "owner" }, h("div", { class: "badge-kind" }, icon(LIB_ICON[item.library_kind] || "folder", "sm")),
      h("div", { class: "t" }, h("div", { style: { fontWeight: 500 } }, item.library), h("div", { class: "muted small", title: item.path }, item.show ? `${item.show} · Season ${item.season}` : item.path))),
    favBtn,
    player && !partyId ? h("button", { class: "btn", onclick: () => startPartyDialog(item, player) }, icon("users"), "Watch together") : null,
    h("button", { class: "btn", onclick: () => addToProjectDialog([id]) }, icon("work"), "Add to project"),
    h("button", { class: "btn", onclick: share }, icon("share"), "Share"),
    h("a", { class: "btn", href: `/api/items/${id}/file?download=1`, download: "" }, icon("download"), "Download"),
    moreBtn));

  // ---- description box
  const m = item.meta || {};
  const facts = [
    ["Captured", fmtDateTime(item.taken_at)], ["Added", timeAgo(item.added_at)],
    ["Duration", fmtDuration(item.duration)], ["Resolution", item.width ? `${item.width}×${item.height} (${resLabel(item.width, item.height)})` : null],
    ["Video", m.vcodec ? `${m.vcodec}${m.fps ? ` · ${m.fps} fps` : ""}${m.hdr ? " · HDR" : ""}` : null],
    ["Audio", m.acodec ? `${m.acodec}${m.channels ? ` · ${m.channels} ch` : ""}` : null],
    ["Bitrate", m.bitrate ? `${(m.bitrate / 1e6).toFixed(1)} Mbps` : null], ["Size", fmtBytes(item.size)],
    ["Camera", m.camera_model || m.model || (m.camera ? `Camera ${m.camera}, reel ${m.reel}, clip ${m.clip}` : null)],
    ["Lens", m.lens], ["ISO", m.iso], ["White balance", m.kelvin ? `${m.kelvin} K` : null], ["Timecode", m.timecode],
    ["Spans", m.spans > 1 ? `${m.spans} R3D files` : null], ["Magazine", m.magazine],
    ["Location", item.lat != null ? `${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}` : null],
    ["Playback", plan.mode === "direct" ? "Original file (direct play)" : plan.reason], ["File", item.path],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");
  const tags = h("div", { class: "tags" });
  const drawTags = () => {
    tags.replaceChildren(...(item.tags || []).map((t) => h("a", { class: "tag", href: `/search?tag=${encodeURIComponent(t)}` }, `#${t}`)),
      h("button", { class: "tag", onclick: addTag }, icon("add", "xs"), "Tag"));
  };
  async function addTag() {
    const t = await promptDialog("Add tag", { placeholder: "e.g. hero shot, b-roll, needs color" });
    if (!t) return;
    const r = await api.patch(`/api/items/${id}`, { add_tags: [t] });
    item.tags = r.tags;
    drawTags();
  }
  drawTags();
  const details = h("dl", { class: "kv", hidden: true }, ...facts.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, String(v))]));
  const showMore = h("button", { class: "btn link sm", onclick: () => { details.hidden = !details.hidden; showMore.textContent = details.hidden ? "…more" : "Show less"; } }, "…more");
  primary.append(h("div", { class: "desc-box" },
    h("div", { class: "meta-line" }, [item.date ? new Date(item.taken_at * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : null,
      item.width ? resLabel(item.width, item.height) : null, m.caption].filter(Boolean).join(" · ")),
    tags, details, showMore,
    item.projects?.length ? h("div", { class: "row wrap", style: { marginTop: "8px" } }, h("span", { class: "muted small" }, "In projects:"),
      ...item.projects.map((p) => h("a", { class: "pill", href: `/project/${p.id}` }, icon("work", "xs"), p.name))) : null));

  // ---- transcript panel (filled by live/complete captions)
  const transcriptBox = h("div", { class: "transcript" });
  const transcriptPanel = h("div", { class: "desc-box", hidden: true }, h("div", { class: "row" }, h("h3", { style: { flex: 1 } }, "Transcript"),
    h("a", { class: "btn ghost sm", href: `/api/items/${id}/captions.vtt`, download: `${item.title}.vtt` }, icon("download", "sm"), "VTT")), transcriptBox);
  primary.append(transcriptPanel);
  let lastCount = 0;
  function drawTranscript(cues) {
    if (!cues.length || cues.length === lastCount) return;
    lastCount = cues.length;
    transcriptPanel.hidden = false;
    transcriptBox.replaceChildren(...cues.map((c) => h("p", { "data-s": c.start, onclick: () => player?.seek(c.start) }, h("time", null, fmtDuration(c.start)), h("span", null, c.text))));
  }
  function highlightTranscript(t) {
    if (transcriptPanel.hidden) return;
    let cur = null;
    for (const p of transcriptBox.children) if (+p.dataset.s <= t) cur = p;
    transcriptBox.querySelector(".cur")?.classList.remove("cur");
    cur?.classList.add("cur");
  }
  if (plan.live_captions?.complete) {
    api.get(`/api/items/${id}/captions/live?at=0`, { quiet: true }).then((r) => r && drawTranscript(r.cues)).catch(() => {});
  }

  // ---- up next
  // ---- queue (yours) + up next (automatic), like YouTube's right column
  const queueBox = h("div");
  const drawQueue = () => {
    const items = queue.items();
    queueBox.replaceChildren();
    if (!items.length) return;
    queueBox.append(h("div", { class: "queue-panel" },
      h("div", { class: "row" }, h("div", { style: { flex: 1 } }, h("b", null, "Queue"), h("div", { class: "muted small" }, `${items.length} video${items.length > 1 ? "s" : ""} · plays next`)),
        h("button", { class: "btn ghost sm", onclick: () => queue.clear() }, "Clear")),
      h("div", { class: "queue-list" }, items.map((it, i) => h("div", { class: "queue-row" },
        h("span", { class: "muted small", style: { width: "18px", textAlign: "center" } }, String(i + 1)),
        h("a", { href: `/watch/${it.id}`, class: "queue-main" }, h("div", { class: "queue-thumb" }, thumbBox(it)),
          h("div", { style: { minWidth: 0 } }, h("div", { class: "queue-title" }, it.title), h("div", { class: "muted small" }, fmtDuration(it.duration)))),
        iconBtn("close", "Remove from queue", () => queue.remove(it.id)))))));
  };
  drawQueue();
  const onQueue = () => drawQueue();
  window.addEventListener("paddock:queue", onQueue);
  secondary.append(queueBox);
  const autoplay = h("input", { type: "checkbox", checked: localStorage.getItem("autoplay") !== "0" });
  autoplay.onchange = () => localStorage.setItem("autoplay", autoplay.checked ? "1" : "0");
  secondary.append(h("div", { class: "row", style: { margin: "4px 0 12px" } }, h("h3", { style: { flex: 1 } }, "Up next"),
    h("label", { class: "row small muted" }, "Autoplay", autoplay)));
  if (upNext.length) {
    const list = h("div", { class: "upnext" });
    for (const r of upNext) list.append(itemCard(r, { badge: false }));
    secondary.append(list);
  } else {
    secondary.append(h("div", { class: "muted small" }, "Nothing else to play yet."));
  }

  async function share() {
    const r = await api.post("/api/share", { item_id: id, hours: 72 });
    copyText(r.url);
    toast("Share link copied — valid 72 h on your network/VPN");
  }

  return { destroy: () => { party?.close(); player?.destroy(); window.removeEventListener("paddock:queue", onQueue); } };
}
