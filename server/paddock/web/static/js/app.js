// Paddock web app: bootstrap, auth screens, shell, router.

import { api } from "./api.js";
import { $, $$, h, clear, icon, iconBtn, menu, toast, debounce, esc, LIB_ICON } from "./ui.js";

export const ctx = { user: null, server: null, libraries: [], aiInfo: null };

const ROUTES = [
  [/^\/$/, "home"],
  [/^\/(movies|shows|music|videos|red|engineering|favorites|collections|search)$/, "browse"],
  [/^\/(show|album|shoot|part|group)\/(.+)$/, "browse"],
  [/^\/(library|collection)\/(\d+)$/, "browse"],
  [/^\/photos$/, "photos"],
  [/^\/watch\/(\d+)$/, "watch"],
  [/^\/item\/(\d+)$/, "item"],
  [/^\/go\/(\d+)$/, "go"],
  [/^\/projects$/, "projects"],
  [/^\/project\/(\d+)$/, "projects"],
  [/^\/assistant$/, "assistant"],
  [/^\/upload$/, "upload"],
  [/^\/account$/, "account"],
  [/^\/trash$/, "account"],
  [/^\/admin(?:\/([\w-]+))?$/, "admin"],
];

let main, currentView = null, navToken = 0;

export function navigate(path, { replace = false } = {}) {
  if (path === location.pathname + location.search && !replace) return render();
  history[replace ? "replaceState" : "pushState"]({}, "", path);
  render();
}

export function setTitle(t) { document.title = t ? `${t} · ${ctx.server?.name || "Paddock"}` : ctx.server?.name || "Paddock"; }

async function render() {
  const token = ++navToken;
  const path = location.pathname;
  document.body.classList.remove("watching", "drawer-open", "searching", "selecting");
  window.scrollTo(0, 0);
  currentView?.destroy?.();
  currentView = null;
  updateNavActive();
  let viewName = "home", match = null;
  for (const [re, name] of ROUTES) {
    const m = path.match(re);
    if (m) { viewName = name; match = m; break; }
  }
  clear(main).append(h("div", { class: "loader" }));
  try {
    const mod = await import(`./views/${viewName}.js`);
    if (token !== navToken) return;
    const root = h("div", { class: "view" });
    const view = await mod.render(root, { match, path, query: new URLSearchParams(location.search) });
    if (token !== navToken) { view?.destroy?.(); return; }
    clear(main).append(root);
    currentView = view || null;
  } catch (e) {
    if (token !== navToken) return;
    console.error(e);
    clear(main).append(h("div", { class: "empty" }, icon("warning"), h("h2", null, "Something went wrong"),
      h("p", null, e.message || String(e)), h("button", { class: "btn", onclick: () => render() }, "Try again")));
  }
}

// ---------- auth screens ----------

function authScreen(setup) {
  const err = h("div", { class: "auth-err" });
  const user = h("input", { class: "input", autocomplete: "username", placeholder: "Username", required: true });
  const pass = h("input", { class: "input", type: "password", autocomplete: setup ? "new-password" : "current-password", placeholder: "Password", required: true });
  const pass2 = setup ? h("input", { class: "input", type: "password", autocomplete: "new-password", placeholder: "Repeat password" }) : null;
  const name = setup ? h("input", { class: "input", placeholder: "Server name", value: "Paddock" }) : null;
  const btn = h("button", { class: "btn accent", type: "submit" }, setup ? "Create admin account" : "Sign in");
  const form = h("form", { class: "stack" }, setup ? name : null, user, pass, pass2, err, btn);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (setup && pass.value !== pass2.value) { err.textContent = "Passwords don't match"; return; }
    btn.disabled = true;
    try {
      if (setup) await api.post("/api/auth/setup", { username: user.value, password: pass.value, server_name: name.value }, { quiet: true });
      else await api.post("/api/auth/login", { username: user.value, password: pass.value }, { quiet: true, allow401: true });
      await boot();
    } catch (ex) {
      err.textContent = ex.message;
      btn.disabled = false;
    }
  });
  const app = $("#app");
  clear(app).append(h("div", { class: "auth" }, h("div", { class: "auth-card" },
    h("div", { class: "logo" }, logoMark(), ctx.server?.name || "Paddock"),
    h("h1", null, setup ? "Welcome to Paddock" : "Sign in"),
    h("p", null, setup ? "Create the first account. It will be the administrator — you can add family members later."
      : "Your private media, footage and engineering vault."),
    form,
    setup ? h("p", { class: "small faint", style: { marginTop: "16px", marginBottom: 0 } },
      "Use 10+ characters. Paddock never sends anything to the internet unless you turn on a cloud AI provider.") : null)));
  user.focus();
}

function logoMark() {
  return h("img", { class: "logo-mark", src: "/static/icon.svg", alt: "", width: 28, height: 28 });
}

// ---------- shell ----------

const NAV = [
  { path: "/", icon: "home", label: "Home" },
  { path: "/movies", icon: "movie", label: "Movies", kind: "movies" },
  { path: "/shows", icon: "tv", label: "TV Shows", kind: "shows" },
  { path: "/music", icon: "music", label: "Music", kind: "music" },
  { path: "/photos", icon: "photo", label: "Photos", kind: "photos" },
  { path: "/videos", icon: "videocam", label: "Home Videos", kind: "home_videos" },
  { path: "/red", icon: "camera", label: "Footage", kind: "red" },
  { path: "/engineering", icon: "build", label: "Engineering", kind: "engineering" },
  "-",
  { path: "/projects", icon: "work", label: "Projects" },
  { path: "/assistant", icon: "sparkle", label: "Assistant" },
  { path: "/favorites", icon: "heart", label: "Favorites", full: true },
  { path: "/collections", icon: "queue", label: "Collections", full: true },
  "-",
  { path: "/account", icon: "user", label: "You" },
  { path: "/admin", icon: "shield", label: "Manage server", admin: true },
];

function shell() {
  const app = $("#app");
  const kinds = new Set(ctx.libraries.map((l) => l.kind));
  const side = h("nav", { class: "sidebar", "aria-label": "Main" });
  for (const n of NAV) {
    if (n === "-") { side.append(h("hr")); continue; }
    if (n.admin && !ctx.user.is_admin) continue;
    if (n.kind && !kinds.has(n.kind) && !(n.kind === "home_videos" && kinds.has("mixed"))) continue;
    side.append(h("a", { href: n.path, "data-nav": n.path, class: n.full ? "full-only" : "" }, icon(n.icon), h("span", { class: "label" }, n.label)));
  }
  side.append(h("div", { class: "foot" }, `${ctx.server.name} · v${ctx.server.version}`, h("br"), "Private · self-hosted"));

  const searchInput = h("input", { type: "search", placeholder: "Search movies, footage, parts, what was said…", "aria-label": "Search", autocomplete: "off" });
  const sugBox = h("div", { class: "suggest", hidden: true });
  const searchForm = h("form", { class: "search", role: "search" }, searchInput,
    h("button", { class: "go", type: "submit", "aria-label": "Search" }, icon("search", "sm")), sugBox);
  searchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    sugBox.hidden = true;
    if (searchInput.value.trim()) navigate(`/search?q=${encodeURIComponent(searchInput.value.trim())}`);
    document.body.classList.remove("searching");
  });
  const suggest = debounce(async () => {
    const q = searchInput.value.trim();
    if (q.length < 2) { sugBox.hidden = true; return; }
    const r = await api.get(`/api/search/suggest?q=${encodeURIComponent(q)}`, { quiet: true }).catch(() => null);
    if (!r || searchInput.value.trim() !== q) return;
    clear(sugBox);
    for (const t of r.tags) sugBox.append(h("a", { href: `/search?tag=${encodeURIComponent(t.tag)}` }, icon("tag", "sm"), h("span", { class: "tag" }, t.tag)));
    for (const it of r.items) {
      sugBox.append(h("a", { href: itemHref(it) }, it.has_thumb ? h("img", { src: `/api/items/${it.id}/thumb`, alt: "", loading: "lazy" }) : icon("search", "sm"), it.title));
    }
    sugBox.hidden = !sugBox.children.length;
  }, 180);
  searchInput.addEventListener("input", suggest);
  searchInput.addEventListener("blur", () => setTimeout(() => (sugBox.hidden = true), 200));

  const initial = (ctx.user.display_name || ctx.user.username || "?")[0];
  const avatar = h("button", { class: "avatar", "aria-label": "Account" }, initial);
  avatar.addEventListener("click", (e) => {
    e.stopPropagation();
    menu(avatar, [
      { head: `Signed in as ${ctx.user.username}` },
      { icon: "user", label: "Your account", fn: () => navigate("/account") },
      { icon: "phone", label: "Connect a phone", fn: () => navigate("/account#devices") },
      ctx.user.is_admin ? { icon: "shield", label: "Manage server", fn: () => navigate("/admin") } : null,
      { icon: "trash", label: "Trash", fn: () => navigate("/trash") },
      { icon: "dark", label: "Toggle theme", fn: toggleTheme },
      "-",
      { icon: "logout", label: "Sign out", fn: signOut },
    ]);
  });

  const jobsBtn = iconBtn("refresh", "Background jobs", () => navigate(ctx.user.is_admin ? "/admin/jobs" : "/account"));
  jobsBtn.classList.add("hide-mobile");
  jobsBtn.hidden = true;

  const top = h("header", { class: "topbar" },
    h("div", { class: "left" },
      iconBtn("menu", "Menu", toggleDrawer),
      h("a", { href: "/", class: "logo", "aria-label": "Home" }, logoMark(), ctx.server.name)),
    h("div", { class: "center" }, iconBtn("back", "Close search", () => document.body.classList.remove("searching"), { class: "show-mobile" }), searchForm),
    h("div", { class: "right" },
      iconBtn("search", "Search", () => { document.body.classList.add("searching"); searchInput.focus(); }, { class: "show-mobile" }),
      jobsBtn,
      iconBtn("upload", "Upload", () => navigate("/upload"), { class: "hide-mobile" }),
      iconBtn("sparkle", "Assistant", () => navigate("/assistant")),
      avatar));

  const bottom = h("nav", { class: "bottomnav", "aria-label": "Quick" },
    h("a", { href: "/", "data-nav": "/" }, icon("home"), "Home"),
    h("a", { href: "/photos", "data-nav": "/photos" }, icon("photo"), "Photos"),
    h("a", { href: "/upload", class: "add", "aria-label": "Upload" }, icon("addCircle")),
    h("a", { href: "/projects", "data-nav": "/projects" }, icon("work"), "Projects"),
    h("a", { href: "/account", "data-nav": "/account" }, icon("user"), "You"));

  main = h("main", { class: "content", id: "main" });
  clear(app).append(top, side, main, bottom);
  pollJobs(jobsBtn);
}

function toggleDrawer() {
  if (innerWidth > 1312) { document.body.classList.toggle("mini"); localStorage.setItem("mini", document.body.classList.contains("mini") ? "1" : ""); return; }
  const open = document.body.classList.toggle("drawer-open");
  $(".scrim")?.remove();
  if (open) {
    const s = h("div", { class: "scrim", onclick: () => { document.body.classList.remove("drawer-open"); s.remove(); } });
    document.body.append(s);
  }
}

function updateNavActive() {
  $(".scrim")?.remove();
  const p = location.pathname;
  for (const a of $$("[data-nav]")) {
    const n = a.dataset.nav;
    a.classList.toggle("active", n === "/" ? p === "/" : p === n || p.startsWith(n + "/"));
  }
}

let jobsTimer;
async function pollJobs(btn) {
  clearTimeout(jobsTimer);
  try {
    const j = await api.get("/api/jobs", { quiet: true });
    btn.hidden = !j.active.length;
    btn.title = j.active.map((x) => `${x.kind}: ${x.message || x.state}`).join("\n");
    btn.querySelector(".badge")?.remove();
    if (j.active.length) btn.append(h("span", { class: "badge" }, j.active.length));
  } catch { /* signed out */ }
  jobsTimer = setTimeout(() => pollJobs(btn), document.hidden ? 30000 : 5000);
}

function toggleTheme() {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const next = cur === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
}

async function signOut() {
  await api.post("/api/auth/logout").catch(() => {});
  ctx.user = null;
  boot();
}

export function itemHref(it) {
  if (["video", "audio", "red_clip", "braw_clip"].includes(it.kind)) return `/watch/${it.id}`;
  return `/item/${it.id}`;
}

export async function refreshLibraries() {
  ctx.libraries = await api.get("/api/libraries");
  return ctx.libraries;
}

// ---------- global drag & drop upload ----------

function setupDrop() {
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    if (!ctx.user || !e.dataTransfer?.types?.includes("Files")) return;
    depth++;
    document.body.classList.add("dragging");
  });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; document.body.classList.remove("dragging"); } });
  window.addEventListener("dragover", (e) => { if (e.dataTransfer?.types?.includes("Files")) e.preventDefault(); });
  window.addEventListener("drop", async (e) => {
    if (!e.dataTransfer?.files?.length || !ctx.user) return;
    e.preventDefault();
    depth = 0;
    document.body.classList.remove("dragging");
    const { queueUploads } = await import("./views/upload.js");
    queueUploads([...e.dataTransfer.files]);
    if (location.pathname !== "/upload") navigate("/upload");
  });
}

// ---------- boot ----------

export async function boot() {
  const theme = localStorage.getItem("theme");
  if (theme) document.documentElement.dataset.theme = theme;
  if (localStorage.getItem("mini")) document.body.classList.add("mini");
  let state;
  try {
    state = await api.get("/api/auth/state", { quiet: true, allow401: true });
  } catch (e) {
    clear($("#app")).append(h("div", { class: "auth" }, h("div", { class: "auth-card" }, h("h1", null, "Can't reach Paddock"),
      h("p", null, e.message), h("button", { class: "btn primary", onclick: boot }, "Retry"))));
    return;
  }
  ctx.server = state.server;
  ctx.zone = state.zone;
  if (state.setup_required) return authScreen(true);
  if (!state.user) return authScreen(false);
  ctx.user = state.user;
  await refreshLibraries();
  shell();
  render();
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download") || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin || url.pathname.startsWith("/api/") || url.pathname.startsWith("/s/")) return;
  e.preventDefault();
  document.body.classList.remove("drawer-open");
  navigate(url.pathname + url.search + url.hash);
});
window.addEventListener("popstate", () => render());
window.addEventListener("paddock:signed-out", () => { if (ctx.user) { ctx.user = null; toast("You were signed out"); boot(); } });
setupDrop();
if ("serviceWorker" in navigator && location.protocol === "https:") {
  // when an updated worker takes over from an older one, reload once so every module comes from the new version
  const hadWorker = !!navigator.serviceWorker.controller;
  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => { if (hadWorker && !reloaded) { reloaded = true; location.reload(); } });
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
boot();
