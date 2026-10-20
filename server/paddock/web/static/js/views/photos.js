// Photos: day-grouped timeline, places, memories, and a full-screen lightbox.

import { api, qs, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle } from "../app.js";
import { addToProjectDialog, bulkActions, enableSelection } from "../cards.js";
import { h, icon, iconBtn, clear, emptyState, fmtDateTime, fmtDuration, toast, fmtBytes, loader } from "../ui.js";

let cleanup = [];

export async function render(root, { query }) {
  cleanup.forEach((f) => f());
  cleanup = [];
  setTitle("Photos");
  const tab = query.get("tab") || "timeline";
  const body = h("div");
  root.append(h("div", { class: "page-head" }, h("h1", null, "Photos"),
    h("div", { class: "actions" }, h("a", { class: "btn", href: "/upload" }, icon("upload"), "Upload"))),
    h("div", { class: "chips" }, ...[["timeline", "Timeline"], ["memories", "On this day"], ["places", "Places"], ["favorites", "Favorites"], ["screens", "Screenshots"]]
      .map(([t, l]) => h("a", { class: `chip ${t === tab ? "active" : ""}`, href: `/photos?tab=${t}` }, l))), body);
  if (tab === "places") return places(body);
  if (tab === "memories") return memories(body);
  timeline(body, tab === "favorites" ? { favorite: true } : tab === "screens" ? { tag: "Screenshot" } : {});
  return { destroy: () => cleanup.forEach((f) => f()) };
}

function timeline(body, extra) {
  const items = [];
  let offset = 0, done = false, busy = false, lastDay = null, grid = null;
  const sentinel = h("div", { style: { height: "1px" } });
  const container = h("div");
  body.append(container, sentinel);
  const sel = enableSelection(container, null, bulkActions(() => location.reload()));
  cleanup.push(sel.destroy);
  const load = async () => {
    if (busy || done) return;
    busy = true;
    const page = await api.get(`/api/items${qs({ kind: "photo,video", sort: "newest", limit: 200, offset, ...extra })}`);
    const filtered = page.filter((p) => p.kind === "photo" || ["photos", "home_videos", "mixed"].includes(p.library_kind));
    if (!offset && !filtered.length) {
      container.append(emptyState("photo", "No photos yet", "Back up your phone with the Paddock app, drop files here, or add a Photos library."));
    }
    for (const p of filtered) {
      const day = p.date ? p.date.slice(0, 10) : "Undated";
      if (day !== lastDay) {
        lastDay = day;
        const d = p.taken_at ? new Date(p.taken_at * 1000) : null;
        container.append(h("div", { class: "day-head" }, h("h3", null, d ? d.toLocaleDateString(undefined, { weekday: "short", month: "long", day: "numeric", year: "numeric" }) : "Undated")));
        grid = h("div", { class: "photo-grid" });
        container.append(grid);
      }
      const idx = items.length;
      items.push(p);
      const tile = h("a", { class: "ph", href: `/item/${p.id}`, "data-id": p.id, onclick: (e) => { if (e.shiftKey || document.body.classList.contains("selecting")) return; e.preventDefault(); lightbox(items, idx); } },
        p.has_thumb ? h("img", { src: thumbUrl(p.id), alt: "", loading: "lazy", decoding: "async" }) : h("div", { class: "fallback" }, icon("photo")),
        p.kind === "video" ? h("span", { class: "corner" }, "▶ ", fmtDuration(p.duration)) : null);
      grid.append(tile);
    }
    offset += page.length;
    done = page.length < 200;
    busy = false;
  };
  const io = new IntersectionObserver((e) => e[0].isIntersecting && load(), { rootMargin: "1200px" });
  io.observe(sentinel);
  cleanup.push(() => io.disconnect());
  load();
}

async function memories(body) {
  const home = await api.get("/api/home");
  const row = home.rows.find((r) => r.key === "memories");
  if (!row) return body.append(emptyState("calendar", "No memories for today", "Photos and videos taken on this date in earlier years will show up here."));
  const byYear = {};
  for (const p of row.items) (byYear[p.date.slice(0, 4)] ||= []).push(p);
  for (const [y, list] of Object.entries(byYear).sort().reverse()) {
    body.append(h("div", { class: "day-head" }, h("h3", null, `${new Date().getFullYear() - +y} years ago · ${y}`)));
    const grid = h("div", { class: "photo-grid" });
    list.forEach((p, i) => grid.append(h("a", { class: "ph", href: "#", onclick: (e) => { e.preventDefault(); lightbox(list, i); } }, h("img", { src: thumbUrl(p.id), alt: "", loading: "lazy" }))));
    body.append(grid);
  }
}

async function places(body) {
  const ps = await api.get("/api/places");
  if (!ps.length) return body.append(emptyState("place", "No geotagged photos yet", "Photos with GPS in their EXIF data are grouped by place here — computed locally, no map service involved."));
  body.append(h("p", { class: "muted" }, "Grouped by ~1 km area from photo GPS data. Paddock doesn't use online maps, so places are shown by coordinates."));
  const grid = h("div", { class: "grid dense" });
  for (const p of ps) {
    grid.append(h("a", { class: "card", href: `/search?q=${encodeURIComponent(`${p.lat.toFixed(2)}`)}`, onclick: async (e) => {
      e.preventDefault();
      const all = await api.get(`/api/items${qs({ kind: "photo,video", limit: 500, sort: "newest" })}`);
      const here = all.filter((x) => x.lat != null && Math.abs(x.lat - p.lat) < 0.006 && Math.abs(x.lon - p.lon) < 0.006);
      if (here.length) lightbox(here, 0);
    } },
      h("div", { class: "thumb" }, h("img", { src: thumbUrl(p.cover_id), alt: "", loading: "lazy" }), h("span", { class: "corner" }, `${p.count}`)),
      h("div", { class: "info" }, h("div", { class: "txt" }, h("div", { class: "title" }, `${p.lat.toFixed(3)}, ${p.lon.toFixed(3)}`),
        h("div", { class: "sub" }, new Date(p.first * 1000).toLocaleDateString(undefined, { month: "short", year: "numeric" }))))));
  }
  body.append(grid);
}

export function lightbox(items, start) {
  let i = start, info = innerWidth > 900 && localStorage.getItem("lb-info") === "1";
  const stage = h("div", { class: "stage" });
  const aside = h("aside", { hidden: !info });
  const counter = h("span", { class: "muted", style: { color: "#ccc", alignSelf: "center", marginLeft: "8px" } });
  const close = () => { box.remove(); document.removeEventListener("keydown", onKey); };
  const favBtn = iconBtn("heartOutline", "Favorite", async () => {
    const it = items[i];
    const r = await api.patch(`/api/items/${it.id}`, { favorite: !it.favorite });
    it.favorite = r.favorite;
    favBtn.replaceChildren(icon(it.favorite ? "heart" : "heartOutline"));
  });
  const box = h("div", { class: "lightbox", role: "dialog", "aria-label": "Photo viewer" },
    h("div", { class: "stage-wrap", style: { flex: 1, position: "relative", display: "flex" } },
      h("div", { class: "top" }, iconBtn("back", "Close", close), counter, h("div", { class: "spacer" }), favBtn,
        iconBtn("work", "Add to project", () => addToProjectDialog([items[i].id])),
        iconBtn("download", "Download original", () => { location.href = `/api/items/${items[i].id}/file?download=1`; }),
        iconBtn("trash", "Move to trash", async () => {
          await api.post("/api/items/trash", { ids: [items[i].id] });
          toast("Moved to trash", { action: { label: "Undo", fn: () => api.post("/api/items/restore", { ids: [items[i].id] }) } });
          items.splice(i, 1);
          if (!items.length) return close();
          show(Math.min(i, items.length - 1));
        }),
        iconBtn("info", "Info", () => { info = !info; aside.hidden = !info; localStorage.setItem("lb-info", info ? "1" : "0"); if (info) drawInfo(); })),
      iconBtn("chevronLeft", "Previous", () => show(i - 1), { class: "nav prev" }),
      stage,
      iconBtn("chevronRight", "Next", () => show(i + 1), { class: "nav next" })),
    aside);
  const onKey = (e) => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(i - 1);
    else if (e.key === "ArrowRight") show(i + 1);
    else if (e.key === "i") { info = !info; aside.hidden = !info; if (info) drawInfo(); }
  };
  document.addEventListener("keydown", onKey);
  // swipe
  let sx = null;
  stage.addEventListener("touchstart", (e) => { sx = e.touches[0].clientX; }, { passive: true });
  stage.addEventListener("touchend", (e) => {
    if (sx === null) return;
    const dx = e.changedTouches[0].clientX - sx;
    if (Math.abs(dx) > 60) show(i + (dx < 0 ? 1 : -1));
    sx = null;
  });
  async function drawInfo() {
    const it = items[i];
    aside.replaceChildren(loader());
    const d = await api.get(`/api/items/${it.id}`);
    const m = d.meta || {};
    const rows = [["Taken", fmtDateTime(d.taken_at)], ["Camera", [m.make, m.model].filter(Boolean).join(" ")], ["Lens", m.lens],
      ["Exposure", [m.f_number ? `ƒ/${m.f_number}` : null, m.exposure ? `${m.exposure < 1 ? `1/${Math.round(1 / m.exposure)}` : m.exposure}s` : null, m.iso ? `ISO ${m.iso}` : null, m.focal_length ? `${m.focal_length}mm` : null].filter(Boolean).join(" · ")],
      ["Size", [d.width ? `${d.width}×${d.height}` : null, fmtBytes(d.size)].filter(Boolean).join(" · ")], ["Location", d.lat != null ? `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}` : null],
      ["File", d.path], ["Library", d.library]].filter(([, v]) => v);
    aside.replaceChildren(h("h3", { style: { marginBottom: "12px" } }, "Details"),
      m.caption ? h("p", { class: "muted" }, icon("sparkle", "xs"), " ", m.caption) : null,
      h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
      h("div", { class: "tags", style: { marginTop: "16px" } }, ...(d.tags || []).map((t) => h("a", { class: "tag", href: `/search?tag=${encodeURIComponent(t)}`, onclick: close }, `#${t}`))),
      d.projects?.length ? h("div", { style: { marginTop: "16px" } }, h("div", { class: "muted small" }, "Projects"),
        ...d.projects.map((p) => h("a", { class: "pill", href: `/project/${p.id}`, onclick: close }, p.name))) : null);
  }
  function show(n) {
    if (n < 0 || n >= items.length) return;
    i = n;
    const it = items[i];
    counter.textContent = `${i + 1} / ${items.length}`;
    favBtn.replaceChildren(icon(it.favorite ? "heart" : "heartOutline"));
    stage.querySelector("video")?.pause();
    if (it.kind === "video") {
      const v = h("video", { controls: true, autoplay: true, playsinline: true, poster: thumbUrl(it.id) });
      api.get(`/api/items/${it.id}/playback`).then((p) => {
        if (p.mode === "direct") v.src = p.url;
        else if (window.Hls?.isSupported()) { const hls = new window.Hls(); hls.loadSource(p.hls); hls.attachMedia(v); }
        else v.src = p.hls;
      });
      stage.replaceChildren(v);
    } else {
      const img = h("img", { src: thumbUrl(it.id), alt: it.title });
      stage.replaceChildren(img);
      const full = new Image();
      full.onload = () => { if (items[i] === it) img.src = full.src; };
      full.src = `/api/items/${it.id}/preview`;
      // preload neighbours
      for (const k of [i - 1, i + 1]) if (items[k]?.kind === "photo") new Image().src = `/api/items/${items[k].id}/preview`;
    }
    if (info) drawInfo();
  }
  document.body.append(box);
  show(i);
}
