import { api } from "../api.js";
import { ctx, setTitle } from "../app.js";
import { groupCard, itemCard, projectCard, shelf } from "../cards.js";
import { h, icon, fmtBytes, emptyState } from "../ui.js";

export async function render(root) {
  setTitle("");
  const data = await api.get("/api/home");
  const hasLibraries = ctx.libraries.length > 0;
  if (!hasLibraries) {
    root.append(emptyState("folderOpen", "Let's add your first library",
      ctx.user.is_admin ? "Point Paddock at folders on your NAS — movies, photos, RED footage, CAD — and it will organise everything."
        : "Ask an admin to add libraries.",
      ctx.user.is_admin ? h("a", { class: "btn accent", href: "/admin/libraries" }, icon("add"), "Add a library") : null));
    return;
  }
  const chips = h("div", { class: "chips" },
    ...[["All", "/"], ["Movies", "/movies"], ["TV", "/shows"], ["Music", "/music"], ["Photos", "/photos"], ["Home videos", "/videos"],
      ["Footage", "/red"], ["Engineering", "/engineering"], ["Projects", "/projects"], ["Favorites", "/favorites"]]
      .map(([l, href], i) => h("a", { class: `chip ${i === 0 ? "active" : ""}`, href }, l)));
  root.append(chips);
  const parties = await api.get("/api/parties", { quiet: true }).catch(() => []);
  if (parties.length) {
    root.append(h("div", { class: "party-strip" }, h("h3", null, icon("users", "sm"), "Watch parties happening now"),
      ...parties.map((p) => h("a", { class: "party-invite", href: `/watch/${p.item_id}?party=${p.id}` },
        h("img", { src: `/api/items/${p.item_id}/thumb`, alt: "", loading: "lazy" }),
        h("div", null, h("b", null, p.title), h("div", { class: "muted small" },
          `${p.host} · ${p.members.length} watching${p.state.playing ? "" : " · paused"}`)),
        h("span", { class: "btn accent sm" }, "Join")))));
  }
  const s = data.stats;
  root.append(h("div", { class: "stats-strip" },
    stat(s.items.toLocaleString(), "items"), stat(fmtBytes(s.bytes), "in your libraries"),
    s.by_kind.video ? stat(s.by_kind.video.duration >= 3600 ? `${Math.round(s.by_kind.video.duration / 3600)} h` : `${Math.max(1, Math.round(s.by_kind.video.duration / 60))} min`, "of video") : null,
    s.by_kind.photo ? stat(s.by_kind.photo.count.toLocaleString(), "photos") : null,
    s.by_kind.red_clip ? stat(s.by_kind.red_clip.count, "RED clips") : null,
    s.projects ? stat(s.projects, "projects") : null));
  for (const row of data.rows) {
    if (row.type === "groups") {
      const square = row.key === "albums";
      root.append(shelf(row.title, row.items.map((g) => groupCard(g, { square })), {
        href: { shows: "/shows", shoots: "/red", albums: "/music" }[row.key], cls: square ? "squares" : "",
      }));
    } else if (row.type === "projects") {
      root.append(shelf(row.title, row.items.map(projectCard), { href: "/projects" }));
    } else {
      const href = { photos: "/photos", new_movies: "/movies", home_videos: "/videos", engineering: "/engineering" }[row.key];
      root.append(shelf(row.title, row.items.map((it) => itemCard(it)), { href }));
    }
  }
}

function stat(v, l) { return h("div", { class: "stat" }, h("b", null, v), h("span", null, l)); }
