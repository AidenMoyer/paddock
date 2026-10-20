// Cards, shelves and grids shared by every page.

import { api, thumbUrl } from "./api.js";
import { itemHref, navigate } from "./app.js";
import { queue } from "./queue.js";
import { h, icon, iconBtn, menu, toast, fmtDuration, fmtDate, timeAgo, fmtBytes, KIND_ICON, promptDialog } from "./ui.js";

const KIND_LABEL = {
  video: "Video", audio: "Audio", photo: "Photo", red_clip: "RED clip", braw_clip: "BRAW clip", model3d: "3D model",
  gcode: "G-code", cad: "CAD", pcb: "PCB", document: "Document",
};

export function thumbBox(it, cls = "") {
  const box = h("div", { class: `thumb ${cls}` });
  if (it.has_thumb) {
    box.append(h("img", { src: thumbUrl(it.id), alt: "", loading: "lazy", decoding: "async" }));
    if (["model3d", "gcode", "cad", "pcb"].includes(it.kind)) box.classList.add("contain");
  } else {
    box.append(h("div", { class: "fallback" }, icon(KIND_ICON[it.kind] || "doc"), it.format || (it.ext || "").replace(".", "").toUpperCase()));
  }
  return box;
}

export function subtitleFor(it) {
  const bits = [];
  switch (it.kind) {
    case "audio": bits.push(it.artist, it.album); break;
    case "red_clip": case "braw_clip":
      bits.push(it.camera_model || (it.camera ? `Cam ${it.camera}${it.reel != null ? ` · Reel ${it.reel}` : ""}` : ""), it.fps ? `${it.fps} fps` : "", fmtDate(it.taken_at)); break;
    case "model3d": case "gcode": case "cad": case "pcb":
      bits.push(it.format || KIND_LABEL[it.kind], it.revision ? `rev ${it.revision}` : "", it.print_time || "", timeAgo(it.added_at)); break;
    default:
      if (it.show) bits.push(`${it.show} · S${it.season || 0}E${it.episode || 0}`);
      else if (it.library_kind === "movies") bits.push(it.year);
      else bits.push(it.library, fmtDate(it.taken_at));
  }
  return bits.filter(Boolean).join(" · ");
}

export function itemCard(it, opts = {}) {
  const box = thumbBox(it, opts.thumbClass || "");
  if (it.duration && ["video", "audio", "red_clip", "braw_clip"].includes(it.kind)) box.append(h("span", { class: "duration" }, fmtDuration(it.duration)));
  else if (it.kind === "photo" && it.width) box.append(h("span", { class: "corner" }, `${it.width}×${it.height}`));
  if (it.progress?.position && it.duration) box.append(h("div", { class: "progress" }, h("i", { style: { width: `${Math.min(100, (100 * it.progress.position) / it.duration)}%` } })));
  if (it.favorite) box.append(h("span", { class: "fav" }, icon("heart", "sm")));
  const more = iconBtn("more", "More", (e) => { e.preventDefault(); e.stopPropagation(); itemMenu(more, it, opts); }, { class: "menu-btn" });
  const card = h("a", { class: "card", href: opts.href || itemHref(it), "data-id": it.id },
    h("span", { class: "select-box" }),
    box,
    h("div", { class: "info" },
      opts.badge !== false ? h("div", { class: "badge-kind" }, icon(KIND_ICON[it.kind] || "doc")) : null,
      h("div", { class: "txt" }, h("div", { class: "title", title: it.title }, it.title), h("div", { class: "sub" }, opts.sub ?? subtitleFor(it))),
      more));
  return card;
}

export function itemMenu(anchor, it, opts = {}) {
  menu(anchor, [
    ["video", "red_clip", "audio"].includes(it.kind) ? { icon: "play", label: "Play", fn: () => navigate(`/watch/${it.id}`) } : null,
    ["video", "red_clip", "braw_clip", "audio"].includes(it.kind) ? { icon: "next", label: "Play next", fn: () => { queue.playNext(it); toast("Plays next"); } } : null,
    ["video", "red_clip", "braw_clip", "audio"].includes(it.kind) ? { icon: "queue", label: "Add to queue", fn: () => { queue.add(it); toast(`Added to queue · ${queue.items().length}`); } } : null,
    "-",
    { icon: it.favorite ? "heartOutline" : "heart", label: it.favorite ? "Remove from favorites" : "Add to favorites", fn: async () => {
      await api.patch(`/api/items/${it.id}`, { favorite: !it.favorite }); it.favorite = !it.favorite; toast(it.favorite ? "Added to favorites" : "Removed");
    } },
    { icon: "work", label: "Add to project…", fn: () => addToProjectDialog([it.id]) },
    { icon: "queue", label: "Add to collection…", fn: () => addToCollectionDialog([it.id]) },
    { icon: "tag", label: "Add tag…", fn: async () => {
      const t = await promptDialog("Add tag", { placeholder: "e.g. b-roll, needs color, PETG" });
      if (t) { await api.patch(`/api/items/${it.id}`, { add_tags: [t] }); toast(`Tagged “${t}”`); }
    } },
    { icon: "download", label: "Download original", fn: () => { location.href = `/api/items/${it.id}/file?download=1`; } },
    "-",
    { icon: "trash", label: "Move to trash", fn: async () => {
      await api.post("/api/items/trash", { ids: [it.id] });
      toast("Moved to trash", { action: { label: "Undo", fn: () => api.post("/api/items/restore", { ids: [it.id] }) } });
      opts.onRemoved?.(it);
    } },
  ]);
}

export async function addToProjectDialog(ids) {
  const { modal } = await import("./ui.js");
  const projects = await api.get("/api/projects");
  const roleSel = h("select", { class: "input" }, ...["footage", "broll", "photo", "audio", "music", "cad", "print", "reference"].map((r) => h("option", { value: r }, r)));
  const list = h("div", { class: "list" });
  const m = modal("Add to project", h("div", null,
    h("div", { class: "field" }, h("label", null, "Role in the project"), roleSel),
    list,
    h("button", { class: "btn", style: { marginTop: "12px" }, onclick: async () => {
      const name = await promptDialog("New project", { placeholder: "e.g. Turbo Manifold Build" });
      if (!name) return;
      const p = await api.post("/api/projects", { name });
      await api.post(`/api/projects/${p.id}/items`, { item_ids: ids, role: roleSel.value });
      m.close(); toast(`Added to “${name}”`, { action: { label: "Open", fn: () => navigate(`/project/${p.id}`) } });
    } }, icon("add"), "New project")));
  for (const p of projects) {
    list.append(h("div", { class: "list-row", onclick: async () => {
      await api.post(`/api/projects/${p.id}/items`, { item_ids: ids, role: roleSel.value });
      m.close(); toast(`Added to “${p.name}”`, { action: { label: "Open", fn: () => navigate(`/project/${p.id}`) } });
    } }, icon("work"), h("div", { class: "t" }, h("div", null, p.name), h("div", { class: "muted small" }, `${p.item_count} items · ${p.status}`))));
  }
  if (!projects.length) list.append(h("p", { class: "muted" }, "No projects yet."));
}

export async function addToCollectionDialog(ids) {
  const { modal } = await import("./ui.js");
  const cols = await api.get("/api/collections");
  const list = h("div", { class: "list" });
  const m = modal("Add to collection", h("div", null, list, h("button", { class: "btn", style: { marginTop: "12px" }, onclick: async () => {
    const name = await promptDialog("New collection", { placeholder: "e.g. Selects reel" });
    if (!name) return;
    await api.post("/api/collections", { name, item_ids: ids });
    m.close(); toast(`Created “${name}”`);
  } }, icon("add"), "New collection")));
  for (const c of cols) {
    list.append(h("div", { class: "list-row", onclick: async () => {
      await api.post(`/api/collections/${c.id}/items`, { ids }); m.close(); toast(`Added to “${c.name}”`);
    } }, icon("queue"), h("div", { class: "t" }, h("div", null, c.name), h("div", { class: "muted small" }, `${c.count} items`))));
  }
}

export function groupHref(g) {
  const [kind] = g.group_key.split(":");
  const route = { show: "show", album: "album", shoot: "shoot", eng: "part" }[kind] || "group";
  return `/${route}/${encodeURIComponent(g.group_key)}`;
}

export function groupCard(g, opts = {}) {
  const [kind] = g.group_key.split(":");
  const box = h("div", { class: `thumb ${opts.square ? "square" : ""}` });
  if (g.cover_id) box.append(h("img", { src: thumbUrl(g.cover_id), alt: "", loading: "lazy" }));
  else box.append(h("div", { class: "fallback" }, icon({ show: "tv", album: "music", shoot: "camera", eng: "cube", day: "photo" }[kind] || "folder")));
  box.append(h("span", { class: "corner" }, `${g.count} ${kind === "album" ? "tracks" : kind === "show" ? "episodes" : kind === "shoot" ? "clips" : kind === "eng" ? "files" : "items"}`));
  const sub = kind === "shoot" ? `${fmtDuration(g.duration)} · ${fmtBytes(g.size)}` :
    kind === "eng" ? `${timeAgo(g.added)}` : kind === "show" ? `Updated ${timeAgo(g.added)}` : fmtDate(g.last);
  return h("a", { class: "card", href: groupHref(g) }, box,
    h("div", { class: "info" }, h("div", { class: "txt" }, h("div", { class: "title" }, g.name), h("div", { class: "sub" }, sub))));
}

export function projectCard(p) {
  const box = h("div", { class: "thumb" });
  if (p.cover_id) box.append(h("img", { src: thumbUrl(p.cover_id), alt: "", loading: "lazy" }));
  else box.append(h("div", { class: "fallback" }, icon("work")));
  box.append(h("span", { class: "corner left top" }, p.status));
  return h("a", { class: "card", href: `/project/${p.id}` }, box,
    h("div", { class: "info" }, h("div", { class: "txt" }, h("div", { class: "title" }, p.name),
      h("div", { class: "sub" }, `${p.item_count} items · updated ${timeAgo(p.updated_at)}`))));
}

export function shelf(title, cards, { href = null, cls = "" } = {}) {
  const track = h("div", { class: "shelf-track" }, cards);
  const prev = h("button", { class: "shelf-nav prev", "aria-label": "Scroll left", hidden: true }, icon("chevronLeft"));
  const next = h("button", { class: "shelf-nav next", "aria-label": "Scroll right" }, icon("chevronRight"));
  prev.onclick = () => track.scrollBy({ left: -track.clientWidth * 0.9, behavior: "smooth" });
  next.onclick = () => track.scrollBy({ left: track.clientWidth * 0.9, behavior: "smooth" });
  const upd = () => {
    prev.hidden = track.scrollLeft < 10;
    next.hidden = track.scrollLeft + track.clientWidth >= track.scrollWidth - 10;
  };
  track.addEventListener("scroll", upd, { passive: true });
  setTimeout(upd, 50);
  return h("section", { class: `shelf ${cls}` },
    h("div", { class: "shelf-head" }, h("h2", null, title), href ? h("a", { href, class: "btn ghost sm" }, "View all") : null),
    h("div", { class: "shelf-body" }, prev, track, next));
}

export function itemGrid(items, opts = {}) {
  const grid = h("div", { class: `grid ${opts.dense ? "dense" : ""}` });
  for (const it of items) grid.append(itemCard(it, opts));
  return grid;
}

// Multi-select on any grid: long-press / shift-click to enter, then act on many items at once.
export function enableSelection(container, getItems, actions) {
  const selected = new Set();
  let bar = null;
  const sync = () => {
    for (const c of container.querySelectorAll("[data-id]")) c.classList.toggle("selected", selected.has(+c.dataset.id));
    if (!selected.size) { bar?.remove(); bar = null; document.body.classList.remove("selecting"); return; }
    document.body.classList.add("selecting");
    if (!bar) {
      bar = h("div", { class: "selection-bar" });
      document.body.append(bar);
    }
    bar.replaceChildren(iconBtn("close", "Clear selection", () => { selected.clear(); sync(); }), h("b", null, `${selected.size} selected`),
      ...actions.map((a) => h("button", { class: "btn sm", onclick: async () => { await a.fn([...selected]); selected.clear(); sync(); } }, icon(a.icon, "sm"), a.label)));
  };
  container.addEventListener("click", (e) => {
    const card = e.target.closest("[data-id]");
    if (!card) return;
    if (selected.size || e.shiftKey || e.ctrlKey || e.metaKey) {
      e.preventDefault();
      e.stopPropagation();
      const id = +card.dataset.id;
      selected.has(id) ? selected.delete(id) : selected.add(id);
      sync();
    }
  }, true);
  let pressTimer;
  container.addEventListener("pointerdown", (e) => {
    const card = e.target.closest("[data-id]");
    if (!card || e.pointerType === "mouse") return;
    pressTimer = setTimeout(() => { selected.add(+card.dataset.id); sync(); navigator.vibrate?.(20); }, 450);
  });
  for (const ev of ["pointerup", "pointermove", "pointercancel"]) container.addEventListener(ev, () => clearTimeout(pressTimer));
  return { clear: () => { selected.clear(); sync(); }, destroy: () => { bar?.remove(); document.body.classList.remove("selecting"); } };
}

export const bulkActions = (onDone) => [
  { icon: "work", label: "Add to project", fn: (ids) => addToProjectDialog(ids) },
  { icon: "queue", label: "Collection", fn: (ids) => addToCollectionDialog(ids) },
  { icon: "tag", label: "Tag", fn: async (ids) => {
    const t = await promptDialog(`Tag ${ids.length} items`, { placeholder: "tag" });
    if (t) { await Promise.all(ids.map((id) => api.patch(`/api/items/${id}`, { add_tags: [t] }))); toast(`Tagged ${ids.length} items`); }
  } },
  { icon: "sparkle", label: "AI tag", fn: async (ids) => { await api.post("/api/ai/autotag", { item_ids: ids }); toast("AI tagging started"); } },
  { icon: "trash", label: "Trash", fn: async (ids) => {
    await api.post("/api/items/trash", { ids });
    toast(`Moved ${ids.length} to trash`, { action: { label: "Undo", fn: () => api.post("/api/items/restore", { ids }) } });
    onDone?.();
  } },
];
