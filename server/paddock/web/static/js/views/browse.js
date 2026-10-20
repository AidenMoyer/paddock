// Browsing: libraries, movies, shows, music, home videos, RED shoot days, engineering, search, favorites, collections.

import { api, qs, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle, itemHref } from "../app.js";
import {
  bulkActions, enableSelection, groupCard, itemCard, itemGrid, addToProjectDialog, thumbBox, groupHref,
} from "../cards.js";
import { playQueue } from "../audio.js";
import {
  h, icon, iconBtn, clear, emptyState, fmtDuration, fmtBytes, fmtDate, timeAgo, toast, loader, modal, field, confirmDialog,
} from "../ui.js";

let cleanup = [];

export async function render(root, { match, query }) {
  cleanup.forEach((f) => f());
  cleanup = [];
  const [, a, b] = match;
  const key = b ? decodeURIComponent(b) : null;
  const view = {
    movies: () => movies(root), shows: () => groups(root, "show:", "TV Shows", "tv"), music: () => music(root),
    videos: () => homeVideos(root), red: () => footage(root), engineering: () => engineering(root, query),
    favorites: () => listing(root, "Favorites", { favorite: true, sort: "added" }, "heart"),
    collections: () => collections(root), search: () => search(root, query),
    show: () => show(root, key), album: () => album(root, key), shoot: () => shoot(root, key), part: () => part(root, key),
    group: () => listing(root, key, { group: key, sort: "group" }), library: () => library(root, +b), collection: () => collection(root, +b),
  }[a];
  await view();
  return { destroy: () => cleanup.forEach((f) => f()) };
}

function head(title, ...actions) {
  setTitle(title);
  return h("div", { class: "page-head" }, h("h1", null, title), h("div", { class: "actions" }, ...actions));
}

function chips(options, active, onpick) {
  const box = h("div", { class: "chips" });
  for (const [val, label] of options) {
    box.append(h("button", { class: `chip ${val === active ? "active" : ""}`, onclick: () => {
      box.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      box.querySelector(`[data-v="${CSS.escape(String(val))}"]`)?.classList.add("active");
      onpick(val);
    }, "data-v": val }, label));
  }
  return box;
}

// Infinite-scrolling grid over /api/items
function pagedGrid(params, opts = {}) {
  const grid = h("div", { class: `grid ${opts.dense ? "dense" : ""}` });
  const sentinel = h("div", { style: { height: "1px" } });
  const wrap = h("div", null, grid, sentinel);
  let offset = 0, done = false, busy = false;
  const page = opts.page || 60;
  const load = async () => {
    if (done || busy) return;
    busy = true;
    const items = await api.get(`/api/items${qs({ ...params, limit: page, offset })}`);
    if (!offset && !items.length) grid.replaceWith(emptyState(opts.emptyIcon || "search", opts.emptyTitle || "Nothing here yet", opts.emptyText));
    for (const it of items) grid.append(itemCard(it, { ...opts, onRemoved: (x) => grid.querySelector(`[data-id="${x.id}"]`)?.remove() }));
    offset += items.length;
    done = items.length < page;
    busy = false;
  };
  const io = new IntersectionObserver((e) => { if (e[0].isIntersecting) load(); }, { rootMargin: "800px" });
  io.observe(sentinel);
  cleanup.push(() => io.disconnect());
  const sel = enableSelection(grid, null, bulkActions(() => location.reload()));
  cleanup.push(sel.destroy);
  load();
  return wrap;
}

async function movies(root) {
  root.append(head("Movies"));
  const body = h("div");
  const show = (sort) => clear(body).append(pagedGrid({ kind: "video", library_id: libOf("movies"), sort }, { emptyIcon: "movie", emptyTitle: "No movies yet" }));
  root.append(chips([["added", "Recently added"], ["title", "A–Z"], ["newest", "Release / date"], ["size", "Largest"]], "added", show), body);
  show("added");
}

function libOf(kind) {
  const libs = ctx.libraries.filter((l) => l.kind === kind);
  return libs.length === 1 ? libs[0].id : undefined;
}

async function groups(root, prefix, title, ic) {
  root.append(head(title));
  const gs = await api.get(`/api/groups${qs({ prefix })}`);
  if (!gs.length) return root.append(emptyState(ic, `No ${title.toLowerCase()} yet`));
  const grid = h("div", { class: "grid" }, gs.map((g) => groupCard(g)));
  root.append(grid);
}

async function show(root, key) {
  const eps = await api.get(`/api/items${qs({ group: key, sort: "group", limit: 500 })}`);
  const name = eps[0]?.show || key.split(":")[1];
  root.append(head(name, h("button", { class: "btn accent", onclick: () => eps[0] && navigate(`/watch/${firstUnwatched(eps).id}`) }, icon("play"), "Play")));
  const seasons = new Map();
  for (const e of eps) {
    const s = e.season ?? 0;
    if (!seasons.has(s)) seasons.set(s, []);
    seasons.get(s).push(e);
  }
  for (const [s, list] of seasons) {
    root.append(h("h2", { style: { margin: "24px 0 12px" } }, s ? `Season ${s}` : "Specials"));
    root.append(h("div", { class: "list" }, list.map((e) => h("a", { class: "list-row", href: `/watch/${e.id}` },
      h("span", { class: "num" }, e.episode ?? ""), e.has_thumb ? h("img", { src: thumbUrl(e.id), alt: "", loading: "lazy" }) : null,
      h("div", { class: "t" }, h("div", null, e.title), h("div", { class: "muted small" }, fmtDuration(e.duration)))))));
  }
}

function firstUnwatched(eps) { return eps.find((e) => !e.progress?.completed) || eps[0]; }

async function music(root) {
  root.append(head("Music"));
  const body = h("div");
  const showAlbums = async () => {
    const gs = await api.get(`/api/groups${qs({ prefix: "album:" })}`);
    clear(body).append(gs.length ? h("div", { class: "grid squares" }, gs.map((g) => groupCard(g, { square: true }))) : emptyState("music", "No music yet"));
  };
  const showSongs = async () => {
    const songs = await api.get(`/api/items${qs({ kind: "audio", sort: "title", limit: 500 })}`);
    clear(body).append(trackList(songs, true));
  };
  root.append(chips([["albums", "Albums"], ["songs", "Songs"]], "albums", (v) => (v === "albums" ? showAlbums() : showSongs())), body);
  showAlbums();
}

function trackList(tracks, showAlbum = false) {
  return h("div", { class: "list" }, tracks.map((t, i) => h("div", { class: "list-row", onclick: () => playQueue(tracks, i) },
    h("span", { class: "num" }, t.track || i + 1),
    h("div", { class: "t" }, h("div", null, t.title), h("div", { class: "muted small" }, [t.artist, showAlbum ? t.album : null].filter(Boolean).join(" · "))),
    h("span", { class: "muted small" }, fmtDuration(t.duration)))));
}

async function album(root, key) {
  const tracks = await api.get(`/api/items${qs({ group: key, sort: "group", limit: 500 })}`);
  const first = tracks[0] || {};
  setTitle(first.album);
  const total = tracks.reduce((s, t) => s + (t.duration || 0), 0);
  root.append(h("div", { class: "album-head" },
    h("div", { class: "art" }, first.has_thumb ? h("img", { src: thumbUrl(first.id), alt: "" }) : h("div", { class: "thumb square" }, icon("music"))),
    h("div", null, h("div", { class: "muted" }, "Album"), h("h1", { style: { fontSize: "36px", margin: "4px 0" } }, first.album || "Album"),
      h("div", { class: "muted" }, `${first.artist || ""} · ${tracks.length} songs · ${fmtDuration(total)}`),
      h("div", { class: "row", style: { marginTop: "16px" } },
        h("button", { class: "btn primary", onclick: () => playQueue(tracks, 0) }, icon("play"), "Play"),
        h("button", { class: "btn", onclick: () => playQueue([...tracks].sort(() => Math.random() - 0.5), 0) }, "Shuffle")))));
  root.append(trackList(tracks));
}

async function homeVideos(root) {
  root.append(head("Home Videos", h("a", { class: "btn", href: "/upload" }, icon("upload"), "Upload")));
  const body = h("div");
  const libs = ctx.libraries.filter((l) => ["home_videos", "photos", "mixed"].includes(l.kind));
  const show = (sort) => {
    clear(body);
    for (const l of libs.length ? libs : [{}]) body.append(pagedGrid({ kind: "video", library_id: l.id, sort }, { emptyIcon: "videocam", emptyTitle: "No home videos yet" }));
  };
  root.append(chips([["newest", "Newest"], ["oldest", "Oldest"], ["duration", "Longest"], ["added", "Recently added"]], "newest", show), body);
  show("newest");
}

// ---------- RED / cinema footage ----------

async function footage(root) {
  root.append(head("Footage",
    ctx.user.is_admin ? h("button", { class: "btn", onclick: offloadDialog }, icon("sd"), "Offload a card") : null));
  const days = await api.get(`/api/groups${qs({ prefix: "shoot:" })}`);
  if (!days.length) {
    root.append(emptyState("camera", "No camera footage yet",
      "Add a RED / Cinema library pointing at your footage dataset. Paddock reads R3D clips (all spans), RMD looks, proxies and BRAW without touching the originals."));
    return;
  }
  const totalDur = days.reduce((s, d) => s + d.duration, 0);
  const totalSize = days.reduce((s, d) => s + d.size, 0);
  root.append(h("p", { class: "muted" }, `${days.length} shoot days · ${days.reduce((s, d) => s + d.count, 0)} clips · ${fmtDuration(totalDur)} · ${fmtBytes(totalSize)}`));
  root.append(h("div", { class: "grid" }, days.map((d) => groupCard(d))));
}

async function offloadDialog() {
  const libs = ctx.libraries.filter((l) => l.kind === "red");
  if (!libs.length) return toast("Create a RED / Cinema library first", { error: true });
  const { pickDirectory } = await import("./admin.js");
  const src = h("input", { class: "input", placeholder: "/mnt/cards/RED_MAG_A001" });
  const label = h("input", { class: "input", placeholder: "e.g. Dyno day" });
  const lib = h("select", { class: "input" }, libs.map((l) => h("option", { value: l.id }, l.name)));
  const m = modal("Offload a camera card", h("div", null,
    h("p", { class: "muted", style: { marginTop: 0 } }, "Copies every file into the library and verifies each one with a BLAKE2b checksum. The card is never modified."),
    field("Card folder (on the NAS)", h("div", { class: "row" }, src, h("button", { class: "btn", onclick: async () => { const p = await pickDirectory(); if (p) src.value = p; } }, "Browse"))),
    field("Shoot name", label), field("Into library", lib)), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      await api.post("/api/red/offload", { source: src.value, library_id: +lib.value, label: label.value || "Shoot" });
      m.close(); toast("Offload started — progress is under background jobs");
    } }, "Start offload")],
  });
}

async function shoot(root, key) {
  const clips = await api.get(`/api/items${qs({ group: key, sort: "group", limit: 500 })}`);
  const day = key.split(":")[1];
  const total = clips.reduce((s, c) => s + (c.duration || 0), 0);
  root.append(head(fmtDate(new Date(day).getTime() / 1000 + 43200, { weekday: "long", year: "numeric", month: "long", day: "numeric" }),
    h("button", { class: "btn", onclick: () => addToProjectDialog(clips.map((c) => c.id)) }, icon("work"), "Add day to project"),
    h("button", { class: "btn", onclick: async () => { await api.post("/api/ai/autotag", { item_ids: clips.map((c) => c.id) }); toast("AI tagging started"); } }, icon("sparkle"), "AI tag")));
  root.append(h("p", { class: "muted" }, `${clips.length} clips · ${fmtDuration(total)} · ${fmtBytes(clips.reduce((s, c) => s + c.size, 0))}`));
  const viewToggle = chips([["grid", "Thumbnails"], ["table", "Clip log"]], "grid", (v) => draw(v));
  const body = h("div");
  root.append(viewToggle, body);
  const draw = (mode) => {
    clear(body);
    if (mode === "grid") { body.append(itemGrid(clips, { dense: true })); return; }
    const rows = clips.map((c) => h("tr", { onclick: () => navigate(itemHref(c)), style: { cursor: "pointer" } },
      h("td", null, c.has_thumb ? h("img", { src: thumbUrl(c.id), style: { width: "96px", borderRadius: "6px" }, loading: "lazy" }) : ""),
      h("td", { class: "mono" }, c.title), h("td", null, c.camera ? `${c.camera}${String(c.reel).padStart(3, "0")}` : ""),
      h("td", null, c.clip ?? ""), h("td", null, fmtDuration(c.duration)), h("td", null, c.width ? `${c.width}×${c.height}` : ""),
      h("td", null, c.fps || ""), h("td", null, c.camera_model || ""), h("td", null, fmtBytes(c.size)),
      h("td", null, c.proxy ? h("span", { class: "pill ok" }, "proxy") : h("span", { class: "pill warn" }, "no proxy"))));
    body.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ...["", "Clip", "Reel", "#", "Dur", "Res", "FPS", "Camera", "Size", ""].map((t) => h("th", null, t)))),
      h("tbody", null, rows))));
  };
  draw("grid");
}

// ---------- engineering ----------

async function engineering(root, query) {
  root.append(head("Engineering", h("a", { class: "btn", href: "/upload?to=engineering" }, icon("upload"), "Add files")));
  const tab = query.get("tab") || "parts";
  const body = h("div");
  root.append(chips([["parts", "Part families"], ["recent", "Recent"], ["model3d", "3D models"], ["gcode", "G-code"], ["cad", "CAD"], ["pcb", "Electronics"]], tab, (v) => draw(v)), body);
  const engLib = ctx.libraries.find((l) => l.kind === "engineering");
  if (engLib) {
    root.insertBefore(h("div", { class: "callout", style: { marginBottom: "8px" } }, icon("info"),
      h("div", null, h("b", null, "Auto-filing is on. "), `Drop CAD, STL, 3MF or G-code into the `,
        h("code", null, `${engLib.options?.inbox || "_Inbox"}`), ` folder of “${engLib.name}” over your SMB share. Put files in a sub-folder named after the project and Paddock files them into `,
        h("code", null, "<Project>/3D Print/…"), " or ", h("code", null, "<Project>/CAD/…"), ", keeping every revision.")), body.previousSibling);
  }
  const draw = async (v) => {
    clear(body).append(loader());
    if (v === "parts") {
      const fams = await api.get(`/api/groups${qs({ prefix: "eng:", limit: 500 })}`);
      clear(body).append(fams.length ? h("div", { class: "grid dense" }, fams.map((g) => groupCard(g))) : emptyState("cube", "No engineering files yet"));
    } else if (v === "recent") {
      clear(body).append(pagedGrid({ kind: "model3d,gcode,cad,pcb", sort: "added" }, { dense: true }));
    } else {
      clear(body).append(pagedGrid({ kind: v, sort: "added" }, { dense: true }));
    }
  };
  draw(tab);
}

async function part(root, key) {
  const files = await api.get(`/api/items${qs({ group: key, sort: "newest", limit: 200 })}`);
  const name = key.split("/").pop();
  root.append(head(name,
    h("button", { class: "btn", onclick: () => addToProjectDialog(files.map((f) => f.id)) }, icon("work"), "Add to project")));
  const folder = key.slice(4, key.lastIndexOf("/"));
  root.append(h("p", { class: "muted" }, `${files.length} files in ${folder || "/"} · every revision and export of this part`));
  const byKind = {};
  for (const f of files) (byKind[f.kind] ||= []).push(f);
  for (const [kind, list] of Object.entries(byKind)) {
    root.append(h("h3", { style: { margin: "20px 0 10px" } }, { model3d: "3D models", gcode: "Sliced G-code", cad: "CAD", pcb: "Electronics", document: "Documents", photo: "Photos", video: "Videos" }[kind] || kind));
    root.append(h("div", { class: "list revisions" }, list.map((f) => h("a", { class: "list-row", href: itemHref(f) },
      f.has_thumb ? h("img", { src: thumbUrl(f.id), loading: "lazy", alt: "" }) : h("div", { style: { width: "72px", display: "grid", placeItems: "center" } }, icon("doc")),
      h("div", { class: "t" }, h("div", null, f.path.split("/").pop()),
        h("div", { class: "muted small" }, [f.format, f.revision ? `rev ${f.revision}` : null, f.print_time, f.filament_type, fmtBytes(f.size), `modified ${timeAgo(f.taken_at)}`].filter(Boolean).join(" · "))),
      f.revision ? h("span", { class: "pill" }, `rev ${f.revision}`) : null))));
  }
}

// ---------- generic ----------

async function library(root, id) {
  const lib = ctx.libraries.find((l) => l.id === id) || (await api.get("/api/libraries")).find((l) => l.id === id);
  root.append(head(lib?.name || "Library", ctx.user.is_admin ? h("button", { class: "btn", onclick: async () => { await api.post(`/api/libraries/${id}/scan`); toast("Scanning…"); } }, icon("refresh"), "Rescan") : null));
  root.append(pagedGrid({ library_id: id, sort: "added" }));
}

async function listing(root, title, params, ic = "search") {
  root.append(head(title.startsWith("folder:") ? title.slice(7) : title));
  root.append(pagedGrid(params, { emptyIcon: ic }));
}

async function collections(root) {
  root.append(head("Collections"));
  const cols = await api.get("/api/collections");
  if (!cols.length) return root.append(emptyState("queue", "No collections yet", "Select items (Shift-click, or long-press on a phone) and choose “Collection”."));
  root.append(h("div", { class: "grid" }, cols.map((c) => h("a", { class: "card", href: `/collection/${c.id}` },
    h("div", { class: "thumb" }, c.cover_id ? h("img", { src: thumbUrl(c.cover_id), alt: "" }) : h("div", { class: "fallback" }, icon("queue")), h("span", { class: "corner" }, `${c.count} items`)),
    h("div", { class: "info" }, h("div", { class: "txt" }, h("div", { class: "title" }, c.name), h("div", { class: "sub" }, c.description || `Created ${timeAgo(c.created_at)}`)))))));
}

async function collection(root, id) {
  const c = await api.get(`/api/collections/${id}`);
  root.append(head(c.name, h("button", { class: "btn danger", onclick: async () => {
    if (await confirmDialog("Delete collection?", "The items stay in your libraries.", { ok: "Delete", danger: true })) {
      await api.del(`/api/collections/${id}`); navigate("/collections");
    }
  } }, "Delete")));
  if (c.description) root.append(h("p", { class: "muted" }, c.description));
  root.append(itemGrid(c.items));
}

async function search(root, query) {
  const q = query.get("q"), tag = query.get("tag");
  root.append(head(tag ? `#${tag}` : `Results for “${q}”`));
  const body = h("div");
  const show = (kind) => clear(body).append(pagedGrid({ query: q, tag, kind: kind || undefined, sort: q ? "relevance" : "newest" },
    { emptyTitle: "No matches", emptyText: "Search covers titles, folders, tags, camera/print metadata and transcribed speech." }));
  root.append(chips([["", "All"], ["video", "Videos"], ["photo", "Photos"], ["audio", "Music"], ["red_clip", "RED clips"], ["model3d,gcode,cad,pcb", "Engineering"]], "", show), body);
  show("");
}
