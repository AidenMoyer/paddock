// Manage server: everything an admin needs, from first setup to day-to-day operations.

import { api, qs } from "../api.js";
import { ctx, navigate, refreshLibraries, setTitle } from "../app.js";
import { pairDialog } from "./account.js";
import {
  h, icon, iconBtn, clear, emptyState, fmtBytes, fmtDateTime, timeAgo, toast, modal, field, confirmDialog, switchEl, copyText,
  loader, LIB_ICON,
} from "../ui.js";

const SECTIONS = [
  ["overview", "dashboard", "Overview"], ["system", "sd", "System & drives"], ["libraries", "folderOpen", "Libraries"], ["users", "users", "Users"],
  ["devices", "phone", "Devices"], ["remote", "lan", "Remote access"], ["servers", "layers", "Linked servers & backup"],
  ["assistant", "sparkle", "Assistant (AI)"], ["agents", "key", "AI agents (MCP)"], ["transcoding", "bolt", "Transcoding"],
  ["transcription", "cc", "Transcription"], ["printers", "printer", "3D printers"], ["jobs", "refresh", "Jobs"],
  ["security", "shield", "Security & audit"], ["general", "settings", "General"],
];

let timers = [];

export async function render(root, { match }) {
  timers.forEach(clearInterval);
  timers = [];
  if (!ctx.user.is_admin) { root.append(emptyState("shield", "Admins only")); return; }
  const section = match[1] || "overview";
  setTitle("Manage server");
  const nav = h("nav", null, SECTIONS.map(([id, ic, label]) => h("a", { href: `/admin/${id}`, class: id === section ? "active" : "" }, icon(ic, "sm"), label)));
  const body = h("div", { class: "admin-body" });
  root.append(h("div", { class: "admin" }, nav, body));
  const fn = { overview, system: systemPage, libraries, users, devices, remote, servers, assistant, agents, transcoding, transcription, printers, jobs, security, general }[section];
  body.append(loader());
  const content = h("div");
  await fn(content);
  clear(body).append(content);
  return { destroy: () => timers.forEach(clearInterval) };
}

const title = (t, d, ...actions) => h("div", null, h("div", { class: "page-head", style: { marginTop: 0 } }, h("h1", null, t), h("div", { class: "actions" }, ...actions)), d ? h("p", { class: "muted", style: { marginTop: "-8px" } }, d) : null);
const panel = (t, d, ...kids) => h("section", { class: "panel" }, t ? h("h3", null, t) : null, d ? h("p", { class: "desc" }, d) : null, ...kids);
const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
const meter = (p) => h("div", { class: `meter ${p > 90 ? "bad" : p > 75 ? "warn" : ""}` }, h("i", { style: { width: `${p}%` } }));

// ---------------- overview ----------------
async function overview(c) {
  const o = await api.get("/api/admin/overview");
  c.append(title(`${o.server.name}`, `Paddock ${o.server.version} · data in ${o.server.data_dir}`));
  const s = o.stats;
  c.append(h("div", { class: "kpis" },
    kpi("photo", "Items", s.items.toLocaleString()), kpi("sd", "Library size", fmtBytes(s.bytes)),
    kpi("phone", "Devices", o.devices), kpi("users", "Users", o.users),
    kpi("bolt", "Transcoding", o.pipeline_short || "CPU"),
    kpi("sparkle", "Assistant", o.ai.provider === "none" ? "Off" : o.ai.status.reachable ? "Ready" : "Unreachable")));

  // setup checklist
  const steps = [
    [o.libraries.length > 0, "Add a library", "/admin/libraries"],
    [o.devices > 0, "Connect your phone", "/admin/devices"],
    [o.network.wireguard.enabled && !!o.network.wireguard.endpoint, "Turn on remote access", "/admin/remote"],
    [o.ai.provider !== "none", "Choose an AI model", "/admin/assistant"],
    [o.stt.engine !== "none", "Turn on live transcription", "/admin/transcription"],
  ];
  if (steps.some(([done]) => !done)) {
    c.append(panel("Finish setting up", null, h("div", { class: "list" }, steps.map(([done, label, href]) => h("a", { class: "list-row", href },
      h("span", { class: `dot ${done ? "ok" : ""}` }), h("div", { class: "t" }, label), done ? h("span", { class: "pill ok" }, "done") : icon("chevronRight", "sm"))))));
  }

  const disks = o.status.disks.map((d) => d.error ? h("div", null, d.path, " — unavailable") : h("div", { style: { marginBottom: "12px" } },
    h("div", { class: "row" }, h("span", { class: "mono", style: { flex: 1 } }, d.path), h("span", { class: "muted small" }, `${d.free_gb} GB free of ${d.total_gb} GB`)), meter(d.used_pct)));
  const mem = o.status.memory;
  c.append(h("div", { class: "panels" },
    panel("Storage", null, ...disks),
    panel("System", null, h("dl", { class: "kv" },
      h("dt", null, "CPU load"), h("dd", null, o.status.load_avg ? o.status.load_avg.map((x) => x.toFixed(2)).join(" / ") + ` (${o.status.cpu_count} cores)` : `${o.status.cpu_count} cores`),
      h("dt", null, "Memory"), h("dd", null, mem ? `${mem.available_gb} GB free of ${mem.total_gb} GB` : "—"),
      h("dt", null, "GPU"), h("dd", null, o.hardware.devices.map((d) => d.vendor).join(", ") || "none detected"),
      h("dt", null, "Encoders"), h("dd", null, o.hardware.working.join(", ") || "CPU (libx264)"),
      h("dt", null, "Transcode cache"), h("dd", null, `${fmtBytes(o.transcode.cache.bytes)} of ${fmtBytes(o.transcode.cache.limit_bytes)}`),
      h("dt", null, "Live transcodes"), h("dd", null, String(o.transcode.active.length)))),
    panel("Remote access", null, h("dl", { class: "kv" },
      h("dt", null, "Tunnel"), h("dd", null, o.network.wireguard.enabled ? h("span", { class: "pill ok" }, "on") : h("span", { class: "pill" }, "off")),
      h("dt", null, "Endpoint"), h("dd", { class: "mono" }, o.network.wireguard.endpoint || "—"),
      h("dt", null, "Home URLs"), h("dd", { class: "mono" }, o.network.lan_urls.join("\n") || "—")),
      h("a", { class: "btn sm", href: "/admin/remote", style: { marginTop: "12px" } }, "Configure"))));
  c.append(panel("Recent jobs", null, jobsTable(o.jobs)));
}

function kpi(ic, l, v) { return h("div", { class: "kpi" }, h("div", { class: "l" }, icon(ic, "xs"), l), h("div", { class: "v" }, v)); }

// ---------------- libraries ----------------
async function libraries(c) {
  const libs = await refreshLibraries();
  c.append(title("Libraries", "Folders on your NAS that Paddock catalogs. Originals are never modified; mount media datasets read-only where you can.",
    h("button", { class: "btn accent", onclick: () => addLibrary(() => navigate("/admin/libraries", { replace: true })) }, icon("add"), "Add library")));
  if (!libs.length) return c.append(emptyState("folderOpen", "No libraries yet", "Add your Movies, Photos, RED footage or CAD folders to get started."));
  // live progress while anything is scanning
  const progress = {};
  const sig = (ls) => ls.map((l) => `${l.id}:${l.scan_state}:${l.item_count}`).join("|");
  let last = sig(libs);
  const poll = async () => {
    const [j, ls] = await Promise.all([api.get("/api/jobs", { quiet: true }).catch(() => null), api.get("/api/libraries", { quiet: true }).catch(() => null)]);
    if (ls && sig(ls) !== last && !document.querySelector(".modal-back")) { last = sig(ls); navigate("/admin/libraries", { replace: true }); return; }
    for (const l of libs) {
      const el = progress[l.id];
      if (!el) continue;
      const job = j?.active.find((x) => x.kind === "scan" && x.target === String(l.id));
      const done = j?.recent.find((x) => x.kind === "scan" && x.target === String(l.id) && x.state !== "running");
      el.replaceChildren(...(job ? [h("div", { class: "muted small" }, job.message || "Starting…"), meter(Math.round(job.progress * 100))]
        : done?.message ? [h("div", { class: "muted small" }, `Last scan: ${done.message}`)] : []));
    }
  };
  timers.push(setInterval(poll, 2500));
  setTimeout(poll, 50);
  for (const l of libs) {
    progress[l.id] = h("div", { style: { marginTop: "8px" } });
    const opts = l.options || {};
    c.append(panel(null, null, h("div", { class: "row wrap" },
      h("div", { class: "badge-kind" }, icon(LIB_ICON[l.kind] || "folder")),
      h("div", { style: { flex: 1, minWidth: "200px" } }, h("h3", null, l.name), h("div", { class: "muted small mono" }, l.path),
        h("div", { class: "muted small" }, `${l.kind_label} · ${l.item_count.toLocaleString()} items · ${l.last_scan ? `scanned ${timeAgo(l.last_scan)}` : "never scanned"} · `,
          h("span", { class: `pill ${l.scan_state === "idle" ? "" : l.scan_state === "missing" ? "bad" : "warn"}` }, l.scan_state))),
      h("a", { class: "btn sm", href: `/library/${l.id}` }, "Browse"),
      h("button", { class: "btn sm", onclick: async () => { await api.post(`/api/libraries/${l.id}/scan`); toast("Scan started"); } }, icon("refresh", "sm"), "Scan"),
      iconBtn("more", "More", (e) => {
        e.stopPropagation();
        import("../ui.js").then(({ menu }) => menu(e.currentTarget || e.target, [
          { icon: "refresh", label: "Full rescan (re-read all metadata)", fn: async () => { await api.post(`/api/libraries/${l.id}/scan?full=true`); toast("Full rescan started"); } },
          { icon: "sparkle", label: "AI tag untagged items", fn: async () => { const r = await api.post(`/api/libraries/${l.id}/autotag`); toast(`Tagging ${r.items} items`); } },
          { icon: "cc", label: "Transcribe all videos", fn: async () => { const r = await api.post(`/api/libraries/${l.id}/transcribe`); toast(`${r.queued_chunks} chunks queued`); } },
          { icon: "bolt", label: "Optimize for phones (pre-transcode)", fn: async () => { await api.post(`/api/libraries/${l.id}/optimize`); toast("Optimizing in the background"); } },
          "-",
          { icon: "trash", label: "Remove library (keeps files)", fn: async () => {
            if (await confirmDialog(`Remove ${l.name}?`, "Paddock forgets this library's catalog, tags and progress. Files on disk are not touched.", { ok: "Remove", danger: true })) {
              await api.del(`/api/libraries/${l.id}`); navigate("/admin/libraries", { replace: true });
            }
          } },
        ]));
      })),
      progress[l.id],
      l.kind === "photos" ? switchEl("Receive phone backups here", !!opts.backup_target, async (on) => { await api.patch(`/api/libraries/${l.id}`, { options: { backup_target: on } }); toast("Saved"); }) : null,
      ["engineering", "mixed"].includes(l.kind) ? switchEl(`Auto-file the ${opts.inbox || "_Inbox"} folder`, opts.auto_file ?? l.kind === "engineering",
        async (on) => { await api.patch(`/api/libraries/${l.id}`, { options: { auto_file: on } }); toast("Saved"); },
        "Files dropped there are moved into <Project>/<Category>/ with revisions kept. Needs a writable dataset.") : null));
  }
}

const KIND_INFO = {
  movies: "Movies — titles & years from file names, local posters", shows: "TV — Show/Season/S01E02 structure",
  music: "Music — tags, albums, cover art", photos: "Photos & phone videos — EXIF, timeline, places",
  home_videos: "Home videos — by date and location", red: "RED / cinema — R3D clips, BRAW, shoot days, proxies",
  engineering: "CAD, STL/3MF, G-code, KiCad — revisions & auto-filing", mixed: "Anything — Paddock sorts by file type",
};

export function pickDirectory(start) {
  return new Promise((resolve) => {
    let current = null, picked = null;
    const box = h("div", { class: "dir-picker" });
    const useBtn = h("button", { class: "btn primary", disabled: true }, "Use this folder");
    const load = async (path) => {
      const r = await api.get(`/api/fs/browse${qs({ path })}`);
      current = r.path;
      picked = r.path;
      useBtn.disabled = !r.path;
      const counts = r.files ? Object.entries(r.files).map(([k, v]) => `${v} ${k}`).join(", ") : "";
      box.replaceChildren(h("div", { class: "crumb" }, r.parent || r.path ? iconBtn("back", "Up", () => load(r.parent || "")) : null, h("span", null, r.path || "Media roots"),
        counts ? h("span", { class: "muted", style: { marginLeft: "auto" } }, counts) : null),
        ...r.dirs.map((d) => h("a", { ondblclick: () => load(d.path), onclick: (e) => {
          box.querySelectorAll("a").forEach((a) => a.classList.remove("sel")); e.currentTarget.classList.add("sel"); picked = d.path; useBtn.disabled = false;
        } }, icon("folder", "sm"), d.name, h("span", { class: "spacer" }), iconBtn("chevronRight", "Open", (e) => { e.stopPropagation(); load(d.path); }))),
        !r.dirs.length ? h("div", { class: "muted", style: { padding: "12px" } }, "No sub-folders") : null);
    };
    let done = false;
    const m = modal("Choose a folder", h("div", null, h("p", { class: "muted", style: { marginTop: 0 } }, "Only datasets mounted into the Paddock app are listed. Double-click to open a folder."), box), {
      wide: true, foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), useBtn],
      onclose: () => { if (!done) resolve(null); },
    });
    useBtn.onclick = () => { done = true; m.close(); resolve(picked || current); };
    load(start || "");
  });
}

function addLibrary(onDone, preset = "") {
  let kind = "movies";
  const name = h("input", { class: "input", placeholder: "e.g. Movies", value: preset ? preset.split(/[\\/]/).pop() : "" });
  const path = h("input", { class: "input", placeholder: "/media/movies", readonly: true, value: preset });
  const kinds = h("div", { class: "kind-pick" });
  for (const [k, desc] of Object.entries(KIND_INFO)) {
    kinds.append(h("button", { class: k === kind ? "sel" : "", title: desc, onclick: (e) => {
      kind = k; kinds.querySelectorAll("button").forEach((b) => b.classList.remove("sel")); e.currentTarget.classList.add("sel");
      if (!name.value) name.placeholder = desc.split(" — ")[0];
      hint.textContent = desc;
    } }, icon(LIB_ICON[k], "sm"), desc.split(" — ")[0].split(" & ")[0]));
  }
  const hint = h("div", { class: "hint" }, KIND_INFO[kind]);
  const m = modal("Add a library", h("div", null,
    field("What's in it?", h("div", null, kinds, hint)),
    field("Folder", h("div", { class: "row" }, path, h("button", { class: "btn", onclick: async () => { const p = await pickDirectory(); if (p) { path.value = p; if (!name.value) name.value = p.split(/[\\/]/).pop(); } } }, icon("folderOpen", "sm"), "Browse"))),
    field("Name", name)), {
    wide: true,
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      if (!path.value) return toast("Choose a folder", { error: true });
      await api.post("/api/libraries", { name: name.value || name.placeholder, kind, path: path.value });
      m.close(); toast("Library added — scanning now"); await refreshLibraries(); onDone?.();
    } }, "Add library")],
  });
}

// ---------------- system & drives (Paddock OS) ----------------
// [label, minimum drives, explanation, usable capacity for n drives of size s]
const POOL_LEVELS = {
  raid1: ["Mirror (RAID 1)", 2, "Every drive holds the same data. Keeps working until only one drive is left.", (n, s) => s],
  raid5: ["Parity (RAID 5)", 3, "Survives one drive failing. You get the space of all drives but one.", (n, s) => (n - 1) * s],
  raid6: ["Double parity (RAID 6)", 4, "Survives two drives failing at once. You get the space of all drives but two.", (n, s) => (n - 2) * s],
  raid10: ["Mirrored stripes (RAID 10)", 4, "Fast, and survives one failure in each mirrored pair. Half the total space.", (n, s) => Math.floor(n / 2) * s],
  raid0: ["Striped (RAID 0)", 2, "Fastest and biggest, but if any one drive fails everything on the pool is lost.", (n, s) => n * s],
};
const fmtUptime = (s) => { const d = Math.floor(s / 86400), hr = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60); return d ? `${d} d ${hr} h` : hr ? `${hr} h ${m} min` : `${m} min`; };
const kvRows = (rows) => rows.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)]);

async function systemPage(c) {
  const ov = await api.get("/api/admin/system");
  const i = ov.info || {};
  c.append(title("System & drives", ov.managed
    ? "The computer Paddock OS runs on: drives and RAID pools, network, name, time and power."
    : "The computer Paddock runs on. Its drives, network and updates are managed by the host system (TrueNAS, Docker or your PC)."));
  if (ov.error) c.append(h("div", { class: "callout bad", style: { marginBottom: "16px" } }, icon("warning"), h("div", { class: "small" }, ov.error)));
  const os = i.paddock_os || {};
  const mem = i.memory || {};
  const hottest = (i.temperatures || []).reduce((a, t) => Math.max(a, t.celsius), 0);
  c.append(panel("This computer", null, h("dl", { class: "kv" }, ...kvRows([
    ["System", ov.managed ? `Paddock OS ${os.PADDOCK_OS_VERSION || ""}${os.PADDOCK_OS_VARIANT === "nvidia" ? " (NVIDIA edition)" : ""}` : i.platform],
    ["Mode", i.live ? h("span", { class: "pill warn" }, "Running from the USB stick — nothing is saved") : null],
    ["Based on", ov.managed ? `${i.debian} · Linux ${i.kernel}` : i.container ? "a container" : null],
    ["Name", i.hostname],
    ["Processor", i.cpu ? `${i.cpu} · ${i.cores} threads` : i.cores ? `${i.cores} threads` : null],
    ["Memory", mem.total ? h("div", null, `${fmtBytes(mem.total - mem.available)} of ${fmtBytes(mem.total)} in use`, meter(pct(mem.total - mem.available, mem.total))) : null],
    ["Running for", i.uptime ? fmtUptime(i.uptime) : null],
    ["Load", i.load ? i.load.map((x) => x.toFixed(2)).join(" · ") : null],
    ["Temperature", hottest ? `${Math.round(hottest)} °C (hottest sensor)` : null],
    ["Clock", ov.managed ? `${i.timezone} · ${i.time_synced ? "synchronised" : "not synchronised yet"}` : null],
    ["Starts with", i.boot],
  ]))));

  if (!ov.managed) {
    c.append(panel("Storage Paddock uses", null, ...(i.storage || []).map((s) => h("div", { style: { marginBottom: "12px" } },
      h("div", { class: "row" }, h("span", { class: "mono small" }, s.label), h("span", { class: "muted small", style: { marginLeft: "auto" } }, `${fmtBytes(s.total - s.free)} of ${fmtBytes(s.total)}`)),
      meter(pct(s.total - s.free, s.total))))));
    return;
  }
  const storage = h("div", null, loader());
  const net = h("div", null, loader());
  c.append(panel("Drives & pools", "Combine drives into a pool for more space and to survive a drive failing. Pools and single drives appear under /srv/media, ready to add as libraries.", storage));
  c.append(panel("Network", null, net));
  c.append(namePanel(i, ov.timezones || []));
  c.append(powerPanel());
  c.append(logsPanel());
  drawStorage(storage);
  drawNetwork(net);
}

function healthPill(s) {
  if (!s || !s.available) return h("span", { class: "muted small" }, "—");
  const extra = [s.temperature ? `${s.temperature} °C` : null, s.power_on_hours ? `${Math.round(s.power_on_hours / 24)} days on` : null].filter(Boolean).join(" · ");
  const worn = (s.reallocated || 0) > 0 || (s.pending || 0) > 0 || (s.media_errors || 0) > 0 || (s.percentage_used || 0) >= 90;
  const pill = s.passed === false ? h("span", { class: "pill bad" }, "Failing — replace soon")
    : worn ? h("span", { class: "pill warn", title: "Reallocated or pending sectors / media errors" }, "Wearing") : h("span", { class: "pill ok" }, "Good");
  return h("div", null, pill, extra ? h("div", { class: "muted small" }, extra) : null);
}

function driveKind(d) {
  if (d.transport === "usb") return "USB";
  if (d.transport === "nvme") return "NVMe SSD";
  return d.rotational ? "Hard drive" : "SSD";
}

async function drawStorage(box) {
  let d;
  try { d = await api.get("/api/admin/system/disks"); } catch (e) { box.replaceChildren(h("p", { class: "muted" }, e.message)); return; }
  const redraw = () => { if (box.isConnected) drawStorage(box); };
  const selected = new Set();
  const createBtn = h("button", { class: "btn accent", disabled: true, onclick: () => createPool(d.disks.filter((x) => selected.has(x.path)), redraw) }, icon("add", "sm"), "Create pool from selected drives");
  const usable = (x) => ["free", "unmounted", "media"].includes(x.role);

  const rows = d.disks.map((x) => {
    const mounted = x.partitions.flatMap((p) => p.mountpoints.filter((m) => m.startsWith("/srv/media/")).map((m) => ({ ...p, mount: m })));
    const use = x.role === "system" ? h("span", { class: "pill" }, "Paddock OS")
      : x.role === "pool" ? h("span", { class: "pill accent" }, `Pool ${x.pools.join(", ")}`)
      : mounted.length ? h("div", null, ...mounted.map((p) => h("div", { class: "mono small" }, p.mount)))
      : x.role === "unmounted" ? h("span", { class: "pill warn" }, "Not mounted") : h("span", { class: "muted small" }, "Empty");
    const actions = [];
    for (const p of mounted) {
      actions.push(h("button", { class: "btn sm", onclick: () => addLibrary(() => navigate("/admin/libraries"), p.mount) }, icon("add", "sm"), "Add as library"));
      actions.push(h("button", { class: "btn sm ghost", onclick: async () => { await api.post("/api/admin/system/disks/unmount", { device: p.path }); toast("Unmounted — safe to unplug"); redraw(); } }, "Unmount"));
    }
    if (x.role === "unmounted") {
      for (const p of x.partitions.filter((p) => p.fstype && !p.mountpoints.length)) {
        actions.push(h("button", { class: "btn sm", onclick: async () => { await api.post("/api/admin/system/disks/mount", { device: p.path }); toast("Mounted"); redraw(); } }, `Mount ${p.label || p.path.split("/").pop()}`));
      }
    }
    if (usable(x)) actions.push(h("button", { class: "btn sm ghost", onclick: () => formatDrive(x, redraw) }, "Erase & format…"));
    const cb = usable(x) ? h("input", { type: "checkbox", "aria-label": `Select ${x.path}`, onchange: (e) => {
      if (e.target.checked) selected.add(x.path); else selected.delete(x.path);
      createBtn.disabled = selected.size < 2;
    } }) : null;
    return h("tr", null, h("td", null, cb),
      h("td", null, h("div", null, x.model || "Drive"), h("div", { class: "muted small mono" }, `${x.path} · ${driveKind(x)}${x.serial ? ` · ${x.serial}` : ""}`)),
      h("td", null, fmtBytes(x.size)), h("td", null, use), h("td", null, healthPill(x.smart)),
      h("td", null, h("div", { class: "row wrap", style: { gap: "6px" } }, ...actions)));
  });

  box.replaceChildren(
    ...d.pools.map((p) => poolCard(p, d.disks, redraw)),
    h("div", { class: "tbl-wrap", style: { marginTop: d.pools.length ? "16px" : 0 } }, h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ...["", "Drive", "Size", "Used for", "Health", ""].map((t) => h("th", null, t)))),
      h("tbody", null, rows))),
    h("div", { class: "row wrap", style: { marginTop: "12px" } }, createBtn,
      h("span", { class: "muted small" }, "Tick two or more empty drives to combine them. Drives in use by Paddock OS can't be selected.")));
  if (d.pools.some((p) => p.action)) setTimeout(redraw, 10000);   // follow rebuild / check progress
}

function poolCard(p, disks, redraw) {
  const [label] = POOL_LEVELS[p.level] || [p.level];
  const state = p.action
    ? h("span", { class: "pill warn" }, `${{ check: "Checking", reshape: "Growing", recovery: "Rebuilding", resync: "Building redundancy" }[p.action] || p.action} ${p.progress ?? 0}%${p.finish ? ` · about ${p.finish} left` : ""}`)
    : p.degraded ? h("span", { class: "pill bad" }, `Degraded — ${p.degraded} drive${p.degraded > 1 ? "s" : ""} missing`) : h("span", { class: "pill ok" }, "Healthy");
  const spares = disks.filter((x) => ["free", "unmounted", "media"].includes(x.role));
  const act = (path, msg, body) => async () => { await api.post(`/api/admin/system/pools/${encodeURIComponent(p.name)}/${path}`, body); toast(msg); redraw(); };
  return h("div", { class: "pool-card" + (p.degraded ? " bad" : "") },
    h("div", { class: "row wrap" }, icon("layers"), h("b", null, p.name), h("span", { class: "muted small" }, `${label} · ${p.devices} drives`), state,
      h("span", { class: "muted small", style: { marginLeft: "auto" } }, p.size ? `${fmtBytes(p.used)} of ${fmtBytes(p.size)} used` : "")),
    p.size ? meter(pct(p.used, p.size)) : null,
    p.degraded ? h("div", { class: "callout bad", style: { margin: "10px 0" } }, icon("warning"), h("div", { class: "small" },
      "A drive in this pool has failed or is missing. Your files are still readable. Add a replacement drive below and the pool rebuilds itself.")) : null,
    h("div", { class: "pool-members" }, ...p.members.map((m) => h("span", { class: `pill${/faulty/.test(m.role) ? " bad" : ""}` },
      `${m.device.replace("/dev/", "")} · ${m.role}`,
      h("button", { class: "icon-btn xs", title: `Remove ${m.device} from the pool`, onclick: async () => {
        const typed = await confirmDialog(`Remove ${m.device} from ${p.name}?`,
          p.level === "raid0" ? "This pool has no redundancy: removing a drive destroys it." : "The pool keeps working without it (degraded) until you add a replacement.",
          { ok: "Remove drive", danger: true, typed: m.device.split("/").pop() });
        if (typed) act("remove", "Drive removed", { device: m.device, confirm: typed })();
      } }, icon("close", "xs"))))),
    h("div", { class: "row wrap", style: { marginTop: "10px", gap: "6px" } },
      p.mountpoint ? h("button", { class: "btn sm", onclick: () => addLibrary(() => navigate("/admin/libraries"), p.mountpoint) }, icon("add", "sm"), "Add as library") : null,
      spares.length ? h("button", { class: "btn sm", onclick: () => addToPool(p, spares, redraw) }, icon("sd", "sm"), p.degraded ? "Replace failed drive" : "Add a drive") : null,
      p.level !== "raid0" && !p.action ? h("button", { class: "btn sm ghost", onclick: act("check", "Integrity check started") }, "Check now") : null,
      !p.action ? h("button", { class: "btn sm ghost", title: "After growing the pool, make the new space usable", onclick: act("expand", "Pool expanded") }, "Use new space") : null,
      h("button", { class: "btn sm danger", onclick: async () => {
        const typed = await confirmDialog(`Delete pool ${p.name}?`, "Every file on this pool is erased and its drives become empty.", { ok: "Delete pool", danger: true, typed: `delete ${p.name}` });
        if (typed) act("delete", "Pool deleted", { confirm: typed })();
      } }, "Delete…")));
}

function addToPool(p, spares, redraw) {
  const pick = h("select", { class: "input" }, spares.map((x) => h("option", { value: x.path }, `${x.model || "Drive"} · ${fmtBytes(x.size)} · ${x.path}`)));
  let grow = false;
  const canGrow = ["raid1", "raid5", "raid6"].includes(p.level) && !p.degraded;
  const m = modal(p.degraded ? `Replace a drive in ${p.name}` : `Add a drive to ${p.name}`, h("div", { class: "stack" },
    field("Drive", pick, "It will be erased."),
    canGrow ? switchEl("Grow the pool (more space)", false, (v) => { grow = v; }, "Off: the drive waits as a hot spare and takes over automatically if another drive fails.") : null,
    p.degraded ? h("p", { class: "muted small" }, "The pool starts rebuilding onto this drive right away.") : null), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      const disk = pick.value;
      m.close();
      const typed = await confirmDialog(`Erase ${disk}?`, "Everything on this drive is erased before it joins the pool.", { ok: "Erase and add", danger: true, typed: disk.split("/").pop() });
      if (!typed) return;
      const r = await api.post(`/api/admin/system/pools/${encodeURIComponent(p.name)}/add`, { disk, grow, confirm: typed });
      toast(r.note || (r.spare ? "Added as a hot spare" : "Added — rebuilding")); redraw();
    } }, "Continue")],
  });
}

function createPool(disks, redraw) {
  const name = h("input", { class: "input", value: "media", maxlength: 31, style: { maxWidth: "220px" } });
  const level = h("select", { class: "input", style: { maxWidth: "320px" } });
  const info = h("p", { class: "muted small" });
  const smallest = Math.min(...disks.map((x) => x.size));
  const n = disks.length;
  for (const [k, [label, min]] of Object.entries(POOL_LEVELS)) {
    level.append(h("option", { value: k, disabled: n < min }, n < min ? `${label} — needs ${min} drives` : label));
  }
  level.value = n >= 3 ? "raid5" : "raid1";
  const describe = () => {
    const [, , text, cap] = POOL_LEVELS[level.value];
    info.textContent = `${text} Usable space: about ${fmtBytes(cap(n, smallest))}${new Set(disks.map((x) => x.size)).size > 1 ? " (each drive counts as the smallest one)" : ""}.`;
  };
  level.addEventListener("change", describe);
  describe();
  const m = modal("Create a pool", h("div", { class: "stack" },
    h("div", { class: "small" }, ...disks.map((x) => h("div", { class: "mono" }, `${x.path} · ${x.model || "Drive"} · ${fmtBytes(x.size)}`))),
    field("Pool name", name, "Lowercase letters, numbers and dashes. It appears as /srv/media/<name>."),
    field("Protection", level), info), {
    wide: true,
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      const nm = name.value.trim().toLowerCase();
      m.close();
      const typed = await confirmDialog(`Erase ${n} drives and create ${nm}?`, "Everything on the selected drives is erased.", { ok: "Create pool", danger: true, typed: nm });
      if (!typed) return;
      toast("Creating the pool…");
      const r = await api.post("/api/admin/system/pools", { name: nm, level: level.value, disks: disks.map((x) => x.path), confirm: typed });
      toast(r.note || "Pool created"); redraw();
    } }, "Create pool")],
  });
}

async function formatDrive(x, redraw) {
  const label = await promptDialog(`Erase and format ${x.path}?`, { label: "Name for the drive (shows up as /srv/media/<name>)", value: "Media", ok: "Next" });
  if (!label) return;
  const typed = await confirmDialog(`Erase ${x.path}?`, `Everything on ${x.model || "this drive"} (${fmtBytes(x.size)}) is erased and it becomes an empty media drive called ${label}.`,
    { ok: "Erase drive", danger: true, typed: x.path.split("/").pop() });
  if (!typed) return;
  toast("Formatting…");
  await api.post("/api/admin/system/disks/format", { disk: x.path, label, confirm: typed });
  toast("Ready"); redraw();
}

async function drawNetwork(box) {
  let n;
  try { n = await api.get("/api/admin/system/network"); } catch (e) { box.replaceChildren(h("p", { class: "muted" }, e.message)); return; }
  let mode = n.mode;
  const current = n.interfaces.flatMap((x) => x.addresses)[0] || "";
  const addr = h("input", { class: "input", value: n.address || current, placeholder: "192.168.1.50/24" });
  const gw = h("input", { class: "input", value: n.gateway || n.default_gateway || "", placeholder: "192.168.1.1" });
  const dns = h("input", { class: "input", value: (n.dns_configured.length ? n.dns_configured : n.dns).join(", "), placeholder: "192.168.1.1" });
  const fixed = h("div", { class: "row wrap" }, field("Address / prefix", addr), field("Router", gw), field("DNS servers", dns, "Up to 3, separated by commas"));
  const picks = h("div", { class: "kind-pick", style: { margin: "12px 0" } });
  const drawPicks = () => {
    picks.replaceChildren(...[["dhcp", "Automatic (from your router)", "lan"], ["static", "Fixed address", "edit"]]
      .map(([v, l, ic]) => h("button", { class: v === mode ? "sel" : "", onclick: () => { mode = v; drawPicks(); } }, icon(ic, "sm"), l)));
    fixed.hidden = mode !== "static";
  };
  drawPicks();
  box.replaceChildren(
    n.pending_revert ? h("div", { class: "callout warn", style: { marginBottom: "12px" } }, icon("warning"),
      h("div", { class: "small", style: { flex: 1 } }, "These network settings are on trial. Keep them, or the previous ones come back on their own."),
      h("button", { class: "btn sm accent", onclick: async () => { await api.post("/api/admin/system/network/confirm"); toast("Network settings kept"); drawNetwork(box); } }, "Keep")) : null,
    h("dl", { class: "kv" }, ...kvRows([
      ...n.interfaces.map((x) => [x.name, `${x.addresses.join(", ") || "no address"}${x.up ? "" : " · cable unplugged"} · ${x.mac}`]),
      ["Router", n.default_gateway], ["DNS", n.dns.join(", ")],
    ])),
    picks, fixed,
    h("button", { class: "btn primary", onclick: async () => {
      const body = { mode, address: addr.value.trim(), gateway: gw.value.trim(), dns: dns.value.split(/[\s,]+/).filter(Boolean) };
      if (!(await confirmDialog("Apply network settings?",
        "Paddock may move to a different address. Open it there and press Keep within 2 minutes, or the previous settings come back on their own.", { ok: "Apply" }))) return;
      await api.put("/api/admin/system/network", body);
      const target = mode === "static" ? body.address.split("/")[0] : location.hostname;
      toast("Applying…");
      setTimeout(() => {
        if (target !== location.hostname) location.href = `${location.protocol}//${target}${location.port ? `:${location.port}` : ""}/admin/system`;
        else drawNetwork(box);
      }, 6000);
    } }, "Apply"));
}

function namePanel(i, zones) {
  const host = h("input", { class: "input", value: i.hostname || "", maxlength: 40, style: { maxWidth: "240px" } });
  const tz = h("select", { class: "input", style: { maxWidth: "320px" } }, zones.map((z) => h("option", { value: z, selected: z === i.timezone }, z.replace(/_/g, " "))));
  return panel("Name & time", null,
    field("Server name", h("div", { class: "row" }, host, h("button", { class: "btn", onclick: async () => {
      const r = await api.put("/api/admin/system/hostname", { hostname: host.value.trim().toLowerCase() });
      toast(`Renamed. Paddock restarts and answers at ${r.url}`);
    } }, "Save")), "Your server answers to <name>.local on your network. Paddock restarts to update its certificate."),
    field("Time zone", h("div", { class: "row" }, tz, h("button", { class: "btn", onclick: async () => {
      await api.put("/api/admin/system/timezone", { timezone: tz.value }); toast("Time zone saved. Paddock restarts.");
    } }, "Save")), "Used for capture dates, schedules and backups."));
}

function powerPanel() {
  const act = (action, t, d, ok, danger) => async () => {
    if (!(await confirmDialog(t, d, { ok, danger }))) return;
    await api.post("/api/admin/system/power", { action });
    toast(`${ok}…`);
  };
  return panel("Power", null, h("div", { class: "row wrap" },
    h("button", { class: "btn", onclick: act("restart-paddock", "Restart Paddock?", "Playback and uploads in progress stop for a few seconds.", "Restart Paddock", false) }, icon("refresh", "sm"), "Restart Paddock"),
    h("button", { class: "btn", onclick: act("reboot", "Restart the computer?", "Paddock is unavailable for a minute or two.", "Restart", true) }, icon("replay10", "sm"), "Restart computer"),
    h("button", { class: "btn danger", onclick: act("poweroff", "Shut down?", "Press the computer's power button to start it again.", "Shut down", true) }, "Shut down")));
}

function logsPanel() {
  const out = h("pre", { class: "logbox" }, "Choose a log and press Show.");
  const unit = h("select", { class: "input", style: { maxWidth: "260px" } },
    [["paddock", "Paddock"], ["paddock-wg", "Tunnel (WireGuard)"], ["systemd-networkd", "Network"], ["systemd-timesyncd", "Clock"], ["kernel", "Kernel (drives, hardware)"]]
      .map(([v, l]) => h("option", { value: v }, l)));
  const load = async () => {
    out.textContent = "Loading…";
    const r = await api.get(`/api/admin/system/logs${qs({ unit: unit.value, lines: 300 })}`).catch((e) => ({ text: e.message }));
    out.textContent = r.text || "(empty)";
    out.scrollTop = out.scrollHeight;
  };
  return panel("Logs", null, h("div", { class: "row", style: { marginBottom: "8px" } }, unit, h("button", { class: "btn", onclick: load }, "Show")), out);
}

// ---------------- users ----------------
async function users(c) {
  const list = await api.get("/api/users");
  c.append(title("Users", "Everyone in the household gets their own login, watch progress and favorites.",
    h("button", { class: "btn accent", onclick: () => {
      const u = h("input", { class: "input", autocomplete: "off" }), p = h("input", { class: "input", type: "password", autocomplete: "new-password" });
      let admin = false;
      const m = modal("Add user", h("div", null, field("Username", u), field("Password", p, "At least 10 characters — they can change it later."),
        switchEl("Administrator", false, (on) => { admin = on; }, "Can manage libraries, users, devices and settings.")), {
        foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => {
          await api.post("/api/users", { username: u.value, password: p.value, is_admin: admin }); m.close(); navigate("/admin/users", { replace: true });
        } }, "Create")],
      });
    } }, icon("add"), "Add user")));
  c.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["User", "Role", "Assistant", "Created", ""].map((t) => h("th", null, t)))),
    h("tbody", null, list.map((u) => h("tr", null, h("td", null, h("div", { class: "row" }, h("span", { class: "avatar", style: { width: "28px", height: "28px" } }, u.username[0]), u.display_name)),
      h("td", null, u.is_admin ? h("span", { class: "pill accent" }, "admin") : "member"), h("td", null, u.allow_ai_changes ? "can edit" : "read-only"),
      h("td", null, timeAgo(u.created_at)),
      h("td", null, u.id !== ctx.user.id ? h("button", { class: "btn danger sm", onclick: async () => {
        if (await confirmDialog(`Delete ${u.username}?`, "Their devices are disconnected and their progress and favorites are removed. Media is not touched.", { ok: "Delete", danger: true })) {
          await api.del(`/api/users/${u.id}`); navigate("/admin/users", { replace: true });
        }
      } }, "Delete") : h("span", { class: "muted small" }, "you"))))))));
}

// ---------------- devices ----------------
async function devices(c) {
  const list = await api.get("/api/devices");
  c.append(title("Devices", "Every paired phone has its own token and its own tunnel key. Revoking one never affects the others.",
    h("button", { class: "btn accent", onclick: () => pairDialog(() => navigate("/admin/devices", { replace: true })) }, icon("phone"), "Connect a phone")));
  let status = null;
  try { status = await api.get("/api/admin/remote", { quiet: true }); } catch { /* ignore */ }
  const hs = Object.fromEntries((status?.tunnel?.peers || []).map((p) => [p.public_key_prefix, p]));
  if (!list.length) return c.append(emptyState("phone", "No devices yet"));
  c.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["Device", "User", "Tunnel", "Last handshake", "Last seen", "Status", ""].map((t) => h("th", null, t)))),
    h("tbody", null, list.map((d) => {
      const peer = d.wg_ip ? Object.values(hs).find((p) => p.allowed_ips?.startsWith(d.wg_ip)) : null;
      return h("tr", null, h("td", null, d.name), h("td", null, d.username), h("td", { class: "mono" }, d.wg_ip || "—"),
        h("td", null, peer?.latest_handshake ? timeAgo(peer.latest_handshake) : "—"),
        h("td", null, d.last_seen ? timeAgo(d.last_seen) : "never"),
        h("td", null, d.revoked_at ? h("span", { class: "pill bad" }, "revoked") : h("span", { class: "pill ok" }, "active")),
        h("td", null, d.revoked_at ? null : h("button", { class: "btn danger sm", onclick: async () => {
          if (await confirmDialog(`Disconnect ${d.name}?`, "The token and tunnel key stop working immediately.", { ok: "Disconnect", danger: true })) {
            await api.del(`/api/devices/${d.id}`); navigate("/admin/devices", { replace: true });
          }
        } }, "Disconnect")));
    })))));
}

// ---------------- remote access ----------------
async function remote(c) {
  const r = await api.get("/api/admin/remote");
  c.append(title("Remote access", "Watch, back up and manage from anywhere without any cloud service. One encrypted UDP port does everything — and Paddock can open it on your router for you."));
  const wg = r.wireguard;
  // status hero
  const ok = wg.enabled && r.endpoint_effective;
  c.append(h("div", { class: `callout ${ok ? "ok" : "warn"}`, style: { marginBottom: "16px" } }, icon(ok ? "check" : "warning"),
    h("div", null, h("b", null, ok ? `Remote access is on — phones connect to ${r.endpoint_effective}` : "Remote access isn't fully set up yet"),
      h("div", { class: "small" }, ok ? "Your router forwards one UDP port to Paddock. Nothing else on your network is exposed, and the tunnel itself can only reach Paddock."
        : !wg.enabled ? "The WireGuard tunnel container isn't enabled. In the TrueNAS app settings set “Remote access” on (PADDOCK_WG_ENABLED=1) and redeploy."
          : "Paddock needs a reachable address: let it open the port on your router (below) or forward it manually."))));

  // step 1: tunnel
  c.append(panel("1 · Private tunnel (WireGuard)", "Only devices and servers you pair can use it. Each gets its own key; unauthenticated packets are silently dropped, so the port is invisible to scanners.",
    h("dl", { class: "kv" },
      h("dt", null, "Status"), h("dd", null, wg.enabled ? (r.tunnel?.up ? h("span", { class: "pill ok" }, "running") : h("span", { class: "pill warn" }, "enabled — waiting for tunnel container")) : h("span", { class: "pill" }, "off")),
      h("dt", null, "UDP port"), h("dd", null, String(wg.port)),
      h("dt", null, "Tunnel network"), h("dd", { class: "mono" }, `${wg.subnet} (Paddock is ${wg.server_ip})`),
      h("dt", null, "Server key"), h("dd", { class: "mono" }, wg.server_public_key || "—"),
      h("dt", null, "Peers"), h("dd", null, `${wg.peers} devices${r.tunnel?.peers ? `, ${r.tunnel.peers.filter((p) => p.latest_handshake && Date.now() / 1000 - p.latest_handshake < 180).length} online now` : ""}`))));

  // step 2: router
  const pm = r.portmap || {};
  const autoSwitch = switchEl("Open the port automatically (UPnP / NAT-PMP)", !!r.auto_portmap, async (on) => {
    await api.put("/api/admin/remote", { auto_portmap: on });
    toast(on ? "Asking your router…" : "Automatic port mapping off");
    setTimeout(() => navigate("/admin/remote", { replace: true }), 1500);
  }, "Paddock asks your router to forward the single UDP port and renews it. Turn it off if your router doesn't support UPnP or you prefer a manual rule.");
  c.append(panel("2 · Your router", null, autoSwitch,
    pm.method ? h("div", { class: `callout ${pm.ok ? "ok" : "bad"}`, style: { margin: "8px 0" } }, icon(pm.ok ? "check" : "warning"),
      h("div", { class: "small" }, pm.ok ? `Mapped via ${pm.method} on ${pm.gateway} — renews automatically. Public IP ${pm.external_ip || "unknown"}.` : `Router didn't accept the mapping: ${pm.error || "unknown error"}. Use the manual rule below.`)) : null,
    h("details", { style: { marginTop: "8px" } }, h("summary", { style: { cursor: "pointer" } }, "Manual port-forward rule"),
      h("p", { class: "muted small" }, "In your router's port-forwarding page, add exactly one rule:"),
      h("pre", { class: "code" }, `Protocol:      UDP\nExternal port: ${wg.port}\nInternal IP:   ${r.lan_ip || "this server's LAN address"}\nInternal port: ${wg.port}`),
      h("p", { class: "muted small" }, "Never forward the web port (8443). Paddock refuses web requests from the internet even if you do."))));

  // step 3: address
  const host = h("input", { class: "input", value: r.endpoint_host || "", placeholder: "Leave empty to use your public IP automatically" });
  c.append(panel("3 · How phones find home", "Without any outside service, Paddock tells your paired phones and linked servers its current public IP whenever they connect, and linked servers relay it if it changes while you're away. If you own a domain or your router has its own dynamic DNS, you can enter it instead.",
    h("dl", { class: "kv", style: { marginBottom: "12px" } },
      h("dt", null, "Public IP"), h("dd", { class: "mono" }, r.public_ip || "unknown"),
      h("dt", null, "Phones connect to"), h("dd", { class: "mono" }, r.endpoint_effective || "—"),
      h("dt", null, "CGNAT"), h("dd", null, r.cgnat ? h("span", { class: "pill bad" }, "your ISP shares your IP — see note") : "not detected")),
    r.cgnat ? h("div", { class: "callout warn", style: { marginBottom: "12px" } }, icon("warning"), h("div", { class: "small" },
      "Your router's WAN address is behind carrier-grade NAT, so nothing at home can be reached from outside. Options: ask your ISP for a public IPv4 (often free), or link a Paddock Node that has a public address (e.g. at a friend's or family member's house) and let it relay your tunnel.")) : null,
    field("Hostname (optional)", host), h("button", { class: "btn primary", onclick: async () => { await api.put("/api/admin/remote", { endpoint_host: host.value }); toast("Saved"); navigate("/admin/remote", { replace: true }); } }, "Save")));

  // LAN
  c.append(panel("At home", "On your Wi-Fi the app talks to Paddock directly over HTTPS — no tunnel, full LAN speed.",
    h("dl", { class: "kv" },
      h("dt", null, "Addresses"), h("dd", { class: "mono" }, r.lan_urls.join("\n")),
      h("dt", null, "Certificate"), h("dd", { class: "mono" }, r.fingerprint || "TLS off"),
      h("dt", null, "Allowed networks"), h("dd", { class: "mono" }, r.allowed_networks.join(", "))),
    h("a", { class: "btn sm", href: "/api/ca.crt", download: "paddock-ca.crt", style: { marginTop: "12px" } }, icon("download", "sm"), "Download certificate for desktop browsers")));
}

// ---------------- linked servers & backup ----------------
async function servers(c) {
  const f = await api.get("/api/federation");
  c.append(title("Linked servers & backup", "Connect other Paddock servers — your own offsite box, a family member's NAS, a friend's server — to back up to each other and share libraries. Every link is end-to-end encrypted and approved on both sides.",
    h("button", { class: "btn", onclick: () => acceptInvite() }, icon("link"), "Join with an invite"),
    h("button", { class: "btn accent", onclick: () => createInvite(f) }, icon("add"), "Invite a server")));

  c.append(panel("This server", null, h("dl", { class: "kv" },
    h("dt", null, "Name"), h("dd", null, f.self.name), h("dt", null, "Role"), h("dd", null, f.self.role === "node" ? "Paddock Node (backup target)" : "Paddock server"),
    h("dt", null, "Identity"), h("dd", { class: "mono" }, f.self.fingerprint)),
    h("p", { class: "muted small", style: { marginBottom: 0 } }, "Setting up an offsite backup box? Install ", h("b", null, "Paddock Node"), " on it — ",
      h("a", { href: "#", style: { color: "var(--link)" }, onclick: (e) => { e.preventDefault(); nodeDownloadDialog(); } }, "get the sister app"), ".")));

  if (!f.peers.length) {
    c.append(emptyState("layers", "No linked servers yet", "Invite another Paddock server or Paddock Node. Tip: for an offsite backup box, pair it at home first so the first (big) backup runs at full LAN speed, then move it."));
  } else {
    for (const p of f.peers) {
      const online = p.last_contact && Date.now() / 1000 - p.last_contact < 900;
      c.append(panel(null, null, h("div", { class: "row wrap" },
        h("div", { class: "badge-kind" }, icon("layers")),
        h("div", { style: { flex: 1, minWidth: "200px" } }, h("h3", null, p.name), h("div", { class: "muted small" },
          `${p.role === "node" ? "Paddock Node" : "Paddock server"} · linked ${timeAgo(p.created_at)} · `, h("span", { class: `dot ${online ? "ok" : "warn"}` }), online ? " online" : ` last seen ${p.last_contact ? timeAgo(p.last_contact) : "never"}`),
          h("div", { class: "muted small mono" }, p.fingerprint)),
        h("button", { class: "btn sm", onclick: async () => { const r = await api.post(`/api/federation/peers/${p.id}/ping`); toast(r.ok ? `Reachable in ${r.ms} ms via ${r.via}` : `Unreachable: ${r.error}`, { error: !r.ok }); } }, "Test"),
        h("button", { class: "btn danger sm", onclick: async () => {
          if (await confirmDialog(`Unlink ${p.name}?`, "Both servers stop trusting each other. Backups already stored there stay until you delete them on that server.", { ok: "Unlink", danger: true })) {
            await api.del(`/api/federation/peers/${p.id}`); navigate("/admin/servers", { replace: true });
          }
        } }, "Unlink")),
        h("div", { class: "panels", style: { marginTop: "12px" } },
          h("div", null, h("b", null, "They give us"), h("ul", { class: "small muted" },
            p.their_grants?.backup_quota_gb ? h("li", null, `${p.their_grants.backup_quota_gb} GB of encrypted backup space`) : h("li", null, "no backup space"),
            p.their_grants?.libraries?.length ? h("li", null, `${p.their_grants.libraries.length} shared libraries`, " · ", h("a", { href: `/admin/servers?browse=${p.id}`, style: { color: "var(--link)" }, onclick: (e) => { e.preventDefault(); browsePeer(p); } }, "browse")) : h("li", null, "no shared libraries"))),
          h("div", null, h("b", null, "We give them"), h("ul", { class: "small muted" },
            h("li", null, p.our_grants?.backup_quota_gb ? `${p.our_grants.backup_quota_gb} GB backup space (${fmtBytes(p.vault_used || 0)} used)` : "no backup space"),
            h("li", null, p.our_grants?.libraries?.length ? `${p.our_grants.libraries.length} shared libraries` : "no shared libraries")),
            h("button", { class: "btn sm", onclick: () => editGrants(p) }, "Change what we share")))));
    }
  }

  // backups
  const b = f.backup;
  const targets = f.peers.filter((p) => p.their_grants?.backup_quota_gb);
  const libSel = new Set(b.libraries || []);
  const tgtSel = new Set(b.targets || []);
  const hour = h("select", { class: "input", style: { width: "auto" } }, Array.from({ length: 24 }, (_, i) => h("option", { value: i, selected: i === b.hour }, `${String(i).padStart(2, "0")}:00`)));
  const keep = h("input", { class: "input", type: "number", min: 1, max: 365, value: b.keep, style: { width: "90px" } });
  const bw = h("input", { class: "input", type: "number", min: 0, value: b.bandwidth_mbps || 0, style: { width: "90px" } });
  let enabled = b.enabled;
  c.append(panel("Encrypted backups", "Your files are split into chunks and encrypted on this server before they leave. Linked servers only ever store unreadable blobs — they can't see names, sizes of individual files, or content. Unchanged data is never sent twice.",
    targets.length ? null : h("div", { class: "callout warn", style: { marginBottom: "12px" } }, icon("info"), h("div", { class: "small" }, "No linked server offers backup space yet. On the other server, give this one a backup quota.")),
    switchEl("Back up automatically every night", enabled, (on) => { enabled = on; }),
    h("div", { class: "row wrap", style: { margin: "8px 0 12px" } }, h("span", null, "Start at"), hour, h("span", null, "· keep"), keep, h("span", null, "snapshots · limit upload to"), bw, h("span", { class: "muted" }, "Mbps (0 = no limit)")),
    h("div", { class: "field" }, h("label", null, "What to back up"), h("div", { class: "row wrap" }, ctx.libraries.map((l) => {
      const cb = h("input", { type: "checkbox", checked: libSel.has(l.id), onchange: (e) => (e.target.checked ? libSel.add(l.id) : libSel.delete(l.id)) });
      return h("label", { class: "chip", style: { display: "inline-flex", gap: "6px", alignItems: "center" } }, cb, l.name);
    }), h("label", { class: "chip", style: { display: "inline-flex", gap: "6px", alignItems: "center" } },
      h("input", { type: "checkbox", checked: b.include_database !== false, onchange: (e) => { b.include_database = e.target.checked; } }), "Paddock database (users, tags, projects)"))),
    h("div", { class: "field" }, h("label", null, "Where to"), h("div", { class: "row wrap" }, targets.map((p) => {
      const cb = h("input", { type: "checkbox", checked: tgtSel.has(p.id), onchange: (e) => (e.target.checked ? tgtSel.add(p.id) : tgtSel.delete(p.id)) });
      return h("label", { class: "chip", style: { display: "inline-flex", gap: "6px", alignItems: "center" } }, cb, p.name);
    }))),
    h("div", { class: "row wrap" },
      h("button", { class: "btn primary", onclick: async () => {
        await api.put("/api/backup/config", { enabled, hour: +hour.value, keep: +keep.value, bandwidth_mbps: +bw.value, libraries: [...libSel], targets: [...tgtSel], include_database: b.include_database !== false });
        toast("Backup settings saved");
      } }, "Save"),
      h("button", { class: "btn", disabled: !targets.length, onclick: async () => { await api.post("/api/backup/run"); toast("Backup started — see Jobs"); } }, icon("upload", "sm"), "Back up now"),
      h("button", { class: "btn", onclick: () => recoveryKeyDialog() }, icon("key", "sm"), "Recovery key")),
    b.last_run ? h("p", { class: "muted small" }, `Last backup ${timeAgo(b.last_run.finished_at)} — ${b.last_run.message}`) : null));

  // snapshots
  if (targets.length) {
    const snapBox = h("div");
    c.append(panel("Snapshots & restore", "Restores go into a new folder — your current files are never overwritten.", snapBox));
    for (const p of targets) {
      const snaps = await api.get(`/api/backup/snapshots?peer=${p.id}`, { quiet: true }).catch(() => null);
      snapBox.append(h("h4", { style: { margin: "12px 0 6px" } }, p.name));
      if (!snaps) { snapBox.append(h("p", { class: "muted small" }, "Unreachable right now.")); continue; }
      if (!snaps.length) { snapBox.append(h("p", { class: "muted small" }, "No snapshots yet.")); continue; }
      snapBox.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
        h("thead", null, h("tr", null, ...["Snapshot", "Files", "Size", ""].map((t) => h("th", null, t)))),
        h("tbody", null, snaps.map((s) => h("tr", null, h("td", null, fmtDateTime(s.created)), h("td", null, s.files?.toLocaleString() ?? "—"), h("td", null, fmtBytes(s.bytes)),
          h("td", null, h("button", { class: "btn sm", onclick: () => restoreDialog(p, s) }, icon("restore", "sm"), "Restore"),
            h("button", { class: "btn ghost sm", onclick: async () => { await api.post("/api/backup/verify", { peer: p.id, snapshot: s.name }); toast("Verification started — see Jobs"); } }, "Verify"))))))));
    }
  }
}

async function createInvite(f) {
  const quota = h("input", { class: "input", type: "number", min: 0, value: 0, style: { width: "120px" } });
  const libs = new Set();
  const out = h("div");
  const m = modal("Invite a server", h("div", null,
    h("p", { class: "muted", style: { marginTop: 0 } }, "Creates a one-time invite valid for 30 minutes. Open “Linked servers” on the other Paddock (or Paddock Node) and choose “Join with an invite”."),
    field("Backup space we offer them (GB)", quota, "0 = they can't back up to us. You can change this later."),
    field("Libraries we share with them (read-only)", h("div", { class: "row wrap" }, ctx.libraries.map((l) => h("label", { class: "chip", style: { display: "inline-flex", gap: "6px", alignItems: "center" } },
      h("input", { type: "checkbox", onchange: (e) => (e.target.checked ? libs.add(l.id) : libs.delete(l.id)) }), l.name)))),
    out), {
    wide: true,
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Close"), h("button", { class: "btn accent", onclick: async (e) => {
      e.currentTarget.disabled = true;
      const r = await api.post("/api/federation/invite", { backup_quota_gb: +quota.value, libraries: [...libs] });
      out.replaceChildren(h("div", { class: "stack", style: { alignItems: "center" } },
        h("div", { class: "qr", html: r.qr_svg }),
        h("textarea", { class: "input mono", readonly: true, style: { minHeight: "90px" } }, r.invite),
        h("button", { class: "btn", onclick: () => copyText(r.invite) }, icon("copy", "sm"), "Copy invite"),
        h("div", { class: "callout", style: { width: "100%" } }, icon("shield"), h("div", { class: "small" },
          "Send it privately (in person, Signal, etc.). It works once and expires in 30 minutes. Before the link is trusted you'll both see the same 6-word safety phrase: ",
          h("b", null, r.safety_words)))));
    } }, "Create invite")],
  });
}

async function acceptInvite() {
  const ta = h("textarea", { class: "input mono", placeholder: "pdk-link:…" });
  const out = h("div");
  const m = modal("Join with an invite", h("div", null, field("Paste the invite from the other server", ta), out), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      out.replaceChildren(loader());
      try {
        const pre = await api.post("/api/federation/inspect", { invite: ta.value.trim() });
        out.replaceChildren(h("div", { class: "callout" }, icon("shield"), h("div", null,
          h("div", null, `Link with “${pre.name}”? It offers ${pre.grants.backup_quota_gb || 0} GB of backup space and ${pre.grants.libraries?.length || 0} shared libraries.`),
          h("div", { class: "small", style: { marginTop: "6px" } }, "Safety phrase — must match what the other admin sees: ", h("b", null, pre.safety_words)))),
          h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "12px" } }, h("button", { class: "btn primary", onclick: async () => {
            out.replaceChildren(loader(), h("p", { class: "muted small", style: { textAlign: "center" } }, "Connecting securely… this can take up to a minute while the tunnel comes up."));
            const r = await api.post("/api/federation/accept", { invite: ta.value.trim() });
            m.close(); toast(`Linked with ${r.name}`); navigate("/admin/servers", { replace: true });
          } }, "Phrases match — link")));
      } catch { out.replaceChildren(); }
      btn.disabled = false;
    } }, "Check invite")],
  });
}

function editGrants(p) {
  const quota = h("input", { class: "input", type: "number", min: 0, value: p.our_grants?.backup_quota_gb || 0, style: { width: "120px" } });
  const libs = new Set(p.our_grants?.libraries || []);
  const m = modal(`Share with ${p.name}`, h("div", null, field("Backup space (GB)", quota),
    field("Shared libraries (read-only)", h("div", { class: "row wrap" }, ctx.libraries.map((l) => h("label", { class: "chip", style: { display: "inline-flex", gap: "6px", alignItems: "center" } },
      h("input", { type: "checkbox", checked: libs.has(l.id), onchange: (e) => (e.target.checked ? libs.add(l.id) : libs.delete(l.id)) }), l.name))))), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => {
      await api.patch(`/api/federation/peers/${p.id}`, { grants: { backup_quota_gb: +quota.value, libraries: [...libs] } }); m.close(); navigate("/admin/servers", { replace: true });
    } }, "Save")],
  });
}

async function browsePeer(p) {
  const items = await api.get(`/api/federation/peers/${p.id}/items?limit=60`);
  const grid = h("div", { class: "grid dense" }, items.map((it) => h("a", { class: "card", href: `/api/federation/peers/${p.id}/items/${it.id}/file`, target: "_blank" },
    h("div", { class: "thumb" }, it.has_thumb ? h("img", { src: `/api/federation/peers/${p.id}/items/${it.id}/thumb`, alt: "", loading: "lazy" }) : h("div", { class: "fallback" }, icon("doc"))),
    h("div", { class: "info" }, h("div", { class: "txt" }, h("div", { class: "title" }, it.title), h("div", { class: "sub" }, it.library))))));
  modal(`Shared by ${p.name}`, items.length ? grid : emptyState("layers", "Nothing shared yet"), { wide: true });
}

async function recoveryKeyDialog() {
  const pw = h("input", { class: "input", type: "password", autocomplete: "current-password" });
  const out = h("div");
  const m = modal("Backup recovery key", h("div", null,
    h("div", { class: "callout warn" }, icon("warning"), h("div", { class: "small" }, "This key decrypts your backups. If this server is ever lost, you need it to restore onto a new one. Store it offline (print it, password manager). Anyone with this key and access to a backup server can read your backups.")),
    field("Confirm your password", pw), out), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Close"), h("button", { class: "btn primary", onclick: async () => {
      const r = await api.post("/api/backup/recovery-key", { password: pw.value });
      out.replaceChildren(h("pre", { class: "code", style: { fontSize: "15px", letterSpacing: "1px" } }, r.key), h("button", { class: "btn", onclick: () => copyText(r.key) }, icon("copy", "sm"), "Copy"));
    } }, "Show key")],
  });
}

async function restoreDialog(peer, snap) {
  const lib = h("select", { class: "input" }, h("option", { value: "" }, "Everything in the snapshot"), (snap.libraries || []).map((l) => h("option", { value: l }, l)));
  const prefix = h("input", { class: "input", placeholder: "Optional sub-folder, e.g. Turbo Manifold/" });
  const m = modal("Restore from backup", h("div", null, h("p", { class: "muted", style: { marginTop: 0 } }, `From ${peer.name}, snapshot of ${fmtDateTime(snap.created)}. Files are written to a new “Restored …” folder inside the data directory.`),
    field("Library", lib), field("Only this path", prefix)), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      const r = await api.post("/api/backup/restore", { peer: peer.id, snapshot: snap.name, library: lib.value || null, prefix: prefix.value || null });
      m.close(); toast(`Restoring into ${r.dest}`);
    } }, "Restore")],
  });
}

function nodeDownloadDialog() {
  modal("Get Paddock Node", h("div", { class: "stack" },
    h("p", { class: "muted", style: { margin: 0 } }, "Paddock Node is the same software in a lighter role: it stores encrypted backups for your servers and can share its own folders. It runs on another NAS, a Raspberry Pi, a Linux box, or a Windows PC."),
    h("h4", null, "TrueNAS / any Docker host"),
    h("pre", { class: "code" }, "docker compose -f compose.node.yaml up -d   # from the paddock/deploy folder"),
    h("h4", null, "Windows PC"),
    h("pre", { class: "code" }, "powershell -ExecutionPolicy Bypass -File install-node.ps1"),
    h("p", { class: "muted small", style: { margin: 0 } }, "Then open the node's web page on that machine, create its admin account, and choose “Join with an invite” using an invite from this server.")), { wide: true });
}

// ---------------- assistant ----------------
async function assistant(c) {
  const cfg = await api.get("/api/admin/ai");
  c.append(title("Assistant (AI)", "Choose the model behind the built-in assistant, auto-tagging and video planning. Local models keep everything on your network."));
  const st = cfg.status;
  let provider = cfg.provider;
  const url = h("input", { class: "input", value: cfg.ollama_url });
  const oaUrl = h("input", { class: "input", value: cfg.openai_url });
  const model = h("input", { class: "input", value: cfg.model, list: "models" });
  const models = h("datalist", { id: "models" }, (st.installed || []).map((m) => h("option", { value: m.name })));
  const vision = h("input", { class: "input", value: cfg.vision_model, placeholder: "Same as main model (recommended for Qwen3.8)" });
  const ctxLen = h("input", { class: "input", type: "number", value: cfg.num_ctx, step: 1024, min: 2048 });
  const keep = h("input", { class: "input", value: cfg.keep_alive });
  const temp = h("input", { class: "input", type: "number", step: 0.1, min: 0, max: 2, value: cfg.temperature });
  const key = h("input", { class: "input", type: "password", placeholder: cfg.claude_key_set ? "•••••••• (saved — enter to replace)" : "sk-ant-…" });
  const effort = h("select", { class: "input" }, ["low", "medium", "high", "xhigh", "max"].map((e) => h("option", { value: e, selected: e === cfg.claude_effort }, e)));
  const flags = { think_chat: cfg.think_chat, think_plan: cfg.think_plan, think_tag: cfg.think_tag, autotag_on_upload: cfg.autotag_on_upload };

  const providers = h("div", { class: "kind-pick" });
  const groups = {};
  const drawProviders = () => {
    providers.replaceChildren(...[["none", "Off", "close"], ["ollama", "Local · Ollama", "memory"], ["openai", "Local · vLLM / llama.cpp", "bolt"], ["claude", "Cloud · Claude", "sparkle"]]
      .map(([p, l, ic]) => h("button", { class: p === provider ? "sel" : "", onclick: () => { provider = p; drawProviders(); } }, icon(ic, "sm"), l)));
    for (const [p, g] of Object.entries(groups)) g.hidden = p !== provider && !(p === "local" && ["ollama", "openai"].includes(provider));
  };
  groups.ollama = h("div", null, field("Ollama URL", url, "The Ollama app on TrueNAS is usually http://<nas-ip>:30068 or http://ollama:11434 on the same Docker network."));
  groups.openai = h("div", null, field("Server URL (OpenAI-compatible)", oaUrl, "vLLM: http://<host>:8000/v1 · llama.cpp server: http://<host>:8080/v1"));
  groups.local = h("div", null,
    h("div", { class: "field" }, h("label", null, "Quick setup"), h("div", { class: "kind-pick" }, cfg.presets.map((pr) => h("button", { title: pr.notes, onclick: () => {
      provider = pr.provider; model.value = pr.model; ctxLen.value = pr.num_ctx; drawProviders(); toast(pr.notes, { ms: 8000 });
    } }, icon("bolt", "sm"), pr.label)))),
    field("Model", h("div", null, model, models), "Qwen3.8 27B (qwen3.8:27b) handles chat, tools, planning and image tagging in one model."),
    field("Vision model", vision), h("div", { class: "row wrap" }, field("Context length (tokens)", ctxLen, "Ollama's default silently truncates — Paddock always sets this."), field("Keep loaded for", keep, "e.g. 30m, 2h, -1 = forever")),
    field("Temperature", temp),
    switchEl("Think before answering in chat", flags.think_chat, (v) => { flags.think_chat = v; }, "Better multi-step answers, a bit slower."),
    switchEl("Think while writing video plans", flags.think_plan, (v) => { flags.think_plan = v; }),
    switchEl("Think while tagging images", flags.think_tag, (v) => { flags.think_tag = v; }, "Usually off — much faster for bulk tagging."));
  groups.claude = h("div", null,
    h("div", { class: "callout warn", style: { marginBottom: "12px" } }, icon("warning"), h("div", { class: "small" }, "With Claude, your messages, the catalog details the assistant looks up, and (for tagging) thumbnails are sent to Anthropic. Everything else stays local.")),
    field("Anthropic API key", key, "Stored on this server only; never shown again."), field("Effort", effort));
  c.append(panel("Model provider", null, providers, groups.ollama, groups.openai, groups.local, groups.claude,
    switchEl("Describe & tag new uploads automatically", flags.autotag_on_upload, (v) => { flags.autotag_on_upload = v; }),
    h("div", { class: "row", style: { marginTop: "12px" } }, h("button", { class: "btn primary", onclick: async () => {
      await api.put("/api/admin/ai", { provider, ollama_url: url.value, openai_url: oaUrl.value, model: model.value, vision_model: vision.value,
        num_ctx: +ctxLen.value, keep_alive: keep.value, temperature: +temp.value, claude_effort: effort.value, claude_api_key: key.value || null, ...flags });
      toast("Assistant settings saved"); navigate("/admin/assistant", { replace: true });
    } }, "Save"))));
  drawProviders();

  // status
  if (provider !== "none") {
    const pull = h("input", { class: "input", placeholder: "qwen3.8:27b", style: { maxWidth: "260px" } });
    c.append(panel("Model server", null,
      st.reachable ? h("div", { class: "callout ok" }, icon("check"), h("div", null, `Connected${st.version ? ` · Ollama ${st.version}` : ""}${st.model_installed === false ? ` — but “${st.model}” isn't installed yet` : ""}`))
        : h("div", { class: "callout bad" }, icon("warning"), h("div", null, "Can't reach the model server. ", st.error || "")),
      ...(st.warnings || []).map((w) => h("div", { class: "callout warn", style: { marginTop: "8px" } }, icon("warning"), h("div", { class: "small" }, w))),
      st.loaded?.length ? h("div", { style: { marginTop: "12px" } }, h("b", null, "Loaded now"), ...st.loaded.map((m) => h("div", { style: { margin: "8px 0" } },
        h("div", { class: "row" }, h("span", { class: "mono", style: { flex: 1 } }, m.name), h("span", { class: "muted small" }, `${m.vram_gb} GB VRAM · ${m.on_gpu_pct}% on GPU · ctx ${m.context || "?"}`)), meter(m.on_gpu_pct || 0)))) : null,
      st.installed?.length ? h("details", { style: { marginTop: "12px" } }, h("summary", { style: { cursor: "pointer" } }, `${st.installed.length} models installed`),
        h("div", { class: "tbl-wrap", style: { marginTop: "8px" } }, h("table", { class: "tbl" }, h("tbody", null, st.installed.map((m) => h("tr", null,
          h("td", { class: "mono" }, m.name), h("td", null, m.params || ""), h("td", null, m.quant || ""), h("td", null, m.size_gb ? `${m.size_gb} GB` : "")))))) ) : null,
      cfg.provider === "ollama" ? h("div", { class: "row wrap", style: { marginTop: "12px" } }, pull,
        h("button", { class: "btn", onclick: async () => { await api.post("/api/admin/ai/pull", { model: pull.value || "qwen3.8:27b" }); toast("Downloading — progress under Jobs"); } }, icon("download", "sm"), "Download model"),
        h("button", { class: "btn", onclick: async () => { await api.post("/api/admin/ai/warm"); toast("Loading model into VRAM…"); } }, "Load now"),
        h("button", { class: "btn ghost", onclick: async () => { await api.post("/api/admin/ai/unload"); toast("Model unloaded — VRAM freed"); } }, "Unload")) : null));
  }
}

// ---------------- agents / MCP ----------------
async function agents(c) {
  const tokens = await api.get("/api/tokens");
  const base = location.origin;
  c.append(title("AI agents (MCP)", "Let agents like OpenClaw, Claude Desktop or your own scripts use Paddock through the Model Context Protocol. Each agent gets its own token and permissions; every change it makes is logged.",
    h("button", { class: "btn accent", onclick: () => newToken(base) }, icon("add"), "Connect an agent")));
  c.append(panel("Endpoint", null, h("pre", { class: "code" }, `${base}/mcp`), h("p", { class: "muted small" }, "Streamable HTTP transport · Authorization: Bearer <token>. Agents on other machines need to be on your network or connected through a Paddock tunnel.")));
  if (!tokens.length) return c.append(emptyState("key", "No agents connected"));
  c.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["Agent", "Permissions", "Created", "Last used", ""].map((t) => h("th", null, t)))),
    h("tbody", null, tokens.map((t) => h("tr", null, h("td", null, t.name), h("td", null, t.scopes.map((s) => h("span", { class: `pill ${s === "admin" ? "accent" : s === "write" ? "warn" : ""}`, style: { marginRight: "4px" } }, s))),
      h("td", null, timeAgo(t.created_at)), h("td", null, t.last_used ? timeAgo(t.last_used) : "never"),
      h("td", null, t.revoked_at ? h("span", { class: "pill bad" }, "revoked") : h("button", { class: "btn danger sm", onclick: async () => { await api.del(`/api/tokens/${t.id}`); navigate("/admin/agents", { replace: true }); } }, "Revoke"))))))));
}

function newToken(base) {
  const name = h("input", { class: "input", placeholder: "e.g. OpenClaw" });
  const scopes = new Set(["read"]);
  const out = h("div");
  const m = modal("Connect an agent", h("div", null, field("Name", name),
    switchEl("Read: search, browse, check status", true, () => {}, "Always on."),
    switchEl("Write: tag, organise, projects, plans, rescans, trash (reversible)", false, (on) => (on ? scopes.add("write") : scopes.delete("write"))),
    switchEl("Admin: list devices & server details", false, (on) => (on ? scopes.add("admin") : scopes.delete("admin"))),
    out), {
    wide: true,
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Close"), h("button", { class: "btn primary", onclick: async (e) => {
      e.currentTarget.disabled = true;
      const r = await api.post("/api/tokens", { name: name.value || "Agent", scopes: [...scopes] });
      const generic = JSON.stringify({ mcpServers: { paddock: { type: "http", url: `${base}/mcp`, headers: { Authorization: `Bearer ${r.token}` } } } }, null, 2);
      const stdio = JSON.stringify({ mcpServers: { paddock: { command: "python", args: ["paddock_mcp_stdio.py"], env: { PADDOCK_URL: base, PADDOCK_TOKEN: r.token, PADDOCK_CA: "paddock-ca.crt" } } } }, null, 2);
      out.replaceChildren(h("div", { class: "callout warn" }, icon("key"), h("div", { class: "small" }, r.note)),
        h("pre", { class: "code" }, r.token), h("button", { class: "btn sm", onclick: () => copyText(r.token) }, icon("copy", "sm"), "Copy token"),
        h("h4", { style: { margin: "16px 0 6px" } }, "HTTP clients (OpenClaw, Claude Code, Cursor…)"), h("pre", { class: "code" }, generic),
        h("h4", { style: { margin: "16px 0 6px" } }, "stdio-only clients"), h("pre", { class: "code" }, stdio),
        h("p", { class: "muted small" }, "The stdio bridge (scripts/paddock_mcp_stdio.py) has no dependencies and verifies Paddock's certificate with paddock-ca.crt from Remote access."));
    } }, "Create token")],
  });
}

// ---------------- transcoding ----------------
async function transcoding(c) {
  const o = await api.get("/api/admin/overview");
  const t = o.transcode;
  c.append(title("Transcoding", "When a device can't play a file as-is, Paddock converts it on the fly. Originals are never touched; converted segments are cached so seeking and re-watching are instant."));
  const hwd = o.hardware;
  const errs = hwd.errors || {};
  const ENC = { nvenc: "NVIDIA NVENC", qsv: "Intel Quick Sync", vaapi: "VAAPI", cuda: "NVIDIA decoding" };
  const unavailable = (v) => !["auto", "none"].includes(v) && !hwd.working.includes(v);
  const hw = h("select", { class: "input", style: { maxWidth: "320px" } }, [["auto", "Automatic (fastest combination)"], ["nvenc", "NVIDIA NVENC"], ["qsv", "Intel Quick Sync"], ["vaapi", "VAAPI (AMD / Intel)"], ["none", "CPU only"]]
    .map(([v, l]) => h("option", { value: v, selected: v === t.settings.hwaccel, disabled: unavailable(v) }, `${l}${unavailable(v) ? (errs[v] ? " — unavailable" : " — not detected") : ""}`)));
  const cache = h("input", { class: "input", type: "number", min: 1, value: t.settings.cache_gb, style: { width: "120px" } });
  const ahead = h("input", { class: "input", type: "number", min: 5, max: 200, value: t.settings.ahead_segments, style: { width: "120px" } });
  const gpu = (d) => h("div", null, d.path.startsWith("/") ? `${d.vendor} (${d.path})` : d.path,
    d.status && d.status !== "OK" ? h("span", { class: "pill bad", style: { marginLeft: "8px" }, title: "Windows reports a problem with this GPU's driver" }, `driver ${d.status.toLowerCase()}`) : null);
  c.append(panel("Hardware acceleration", null,
    h("dl", { class: "kv", style: { marginBottom: "12px" } },
      h("dt", null, "GPUs found"), h("dd", null, hwd.devices.length ? hwd.devices.map(gpu) : "none — pass /dev/dri or an NVIDIA GPU to the app"),
      h("dt", null, "Verified encoders"), h("dd", null, hwd.working.map((m) => ENC[m] || m).join(", ") || "none (CPU fallback)"),
      h("dt", null, "GPU decoding"), h("dd", null, (hwd.decode || []).includes("cuda") ? "NVIDIA (CUDA)" : "—"),
      h("dt", null, "In use"), h("dd", null, h("b", null, o.pipeline || o.hwaccel_active))),
    ...Object.entries(errs).map(([k, why]) => h("div", { class: "callout", style: { marginBottom: "8px" } }, icon("info"),
      h("div", { class: "small" }, h("b", null, `${ENC[k] || k} isn't used. `), why))),
    field("Encoder", hw, "Automatic picks the fastest working mix, e.g. NVIDIA decoding with Intel encoding. HDR is tone-mapped to SDR. If the GPU fails on a file Paddock retries on the CPU automatically."),
    h("div", { class: "row wrap" }, field("Cache size limit (GB)", cache), field("Encode ahead (6-second segments)", ahead, "Encoders pause when this far ahead of the viewer.")),
    h("div", { class: "row wrap" },
      h("button", { class: "btn primary", onclick: async () => { const r = await api.put("/api/admin/transcode", { hwaccel: hw.value, cache_gb: +cache.value, ahead_segments: +ahead.value }); toast(`Saved — using ${r.active}`); } }, "Save"),
      h("button", { class: "btn", onclick: async () => { const r = await api.post("/api/admin/transcode/detect"); toast(`Working encoders: ${r.working.join(", ") || "none"}`); navigate("/admin/transcoding", { replace: true }); } }, icon("refresh", "sm"), "Re-detect GPUs"))));
  c.append(panel("Cache", `${fmtBytes(t.cache.bytes)} used of ${fmtBytes(t.cache.limit_bytes)} · ${t.cache.entries} titles`, meter(pct(t.cache.bytes, t.cache.limit_bytes)),
    h("button", { class: "btn", style: { marginTop: "12px" }, onclick: async () => { await api.post("/api/admin/transcode/clear-cache"); toast("Cache cleared"); navigate("/admin/transcoding", { replace: true }); } }, "Clear cache")));
  const act = h("div");
  const drawActive = async () => {
    const ov = await api.get("/api/admin/overview", { quiet: true }).catch(() => null);
    if (!ov) return;
    act.replaceChildren(ov.transcode.active.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ...["Stream", "Encoder", "Encoded to", "Viewer at", "State"].map((x) => h("th", null, x)))),
      h("tbody", null, ov.transcode.active.map((a) => h("tr", null, h("td", { class: "mono" }, a.dir), h("td", null, a.method), h("td", null, `seg ${a.at_segment}`), h("td", null, `seg ${a.viewer_segment}`),
        h("td", null, a.paused ? h("span", { class: "pill" }, "paused (far enough ahead)") : h("span", { class: "pill ok" }, "encoding"))))))) : h("p", { class: "muted" }, "Nothing is transcoding right now."));
  };
  c.append(panel("Live transcodes", null, act));
  drawActive();
  timers.push(setInterval(drawActive, 4000));
}

// ---------------- transcription ----------------
async function transcription(c) {
  const cfg = await api.get("/api/admin/stt");
  c.append(title("Transcription", "Live captions for any video, on the web and in the app, generated on your own hardware. Finished transcripts make spoken words searchable."));
  let engine = cfg.engine;
  const url = h("input", { class: "input", value: cfg.server_url });
  const model = h("input", { class: "input", value: cfg.server_model });
  const ffModel = h("input", { class: "input", value: cfg.ffmpeg_model });
  const lang = h("input", { class: "input", value: cfg.language, style: { width: "120px" } });
  let translate = cfg.translate;
  const picks = h("div", { class: "kind-pick" });
  const gServer = h("div", null, field("Speech server URL", url, "speaches / faster-whisper-server (GPU image) on your network, e.g. http://speaches:8000/v1"),
    field("Model", model, "deepdml/faster-whisper-large-v3-turbo-ct2 is fast and accurate on a GPU; Systran/faster-distil-whisper-small.en for CPU."),
    h("button", { class: "btn sm", onclick: async () => { await api.post("/api/admin/stt/download", { model: model.value }); toast("Model download started"); } }, icon("download", "sm"), "Download model on server"));
  // one-click whisper.cpp models: downloaded once, checksum-verified, then everything runs offline
  const st = cfg.status;
  const models = h("div");
  const bar = (p) => h("div", { class: "meter" }, h("i", { style: { width: `${p}%` } }));
  const drawModels = (running = []) => models.replaceChildren(...(st.models || []).map((m) => {
    const job = running.find((j) => j.target === m.name);
    return h("div", { class: `model-row${m.active ? " active" : ""}` },
      h("div", { class: "grow" },
        h("div", null, h("b", null, m.label), m.active ? h("span", { class: "pill ok", style: { marginLeft: "8px" } }, "In use") : null),
        h("div", { class: "muted small" }, `${fmtBytes(m.size)} · ${m.note}`),
        job ? h("div", { style: { marginTop: "8px" } }, h("div", { class: "muted small" }, job.message || "Starting…"), bar(Math.round(job.progress * 100))) : null),
      job ? null : m.installed ? (m.active ? null : h("button", { class: "btn sm", onclick: async () => {
        await api.put("/api/admin/stt/whisper-model", { name: m.name }); toast(`Using ${m.label}`); navigate("/admin/transcription", { replace: true });
      } }, "Use")) : h("button", { class: "btn sm", onclick: async () => {
        const r = await api.post("/api/admin/stt/whisper-model", { name: m.name }); watching.add(r.job_id); toast(`Downloading ${m.label}`); poll();
      } }, icon("download", "sm"), "Download"));
  }));
  const watching = new Set();
  const poll = async () => {
    const j = await api.get("/api/jobs", { quiet: true }).catch(() => null);
    if (!j) return;
    const running = j.active.filter((x) => x.kind === "stt-model");
    running.forEach((x) => watching.add(x.id));
    const finished = j.recent.filter((x) => watching.has(x.id) && x.state !== "running" && x.state !== "queued");
    if (finished.length && !document.querySelector(".modal-back")) {
      const bad = finished.find((x) => x.state === "failed");
      toast(bad ? `Download failed: ${bad.message}` : finished[0].message || "Model ready", { error: !!bad });
      navigate("/admin/transcription", { replace: true });
      return;
    }
    drawModels(running);
  };
  drawModels();
  poll();
  timers.push(setInterval(poll, 2000));
  const gFf = h("div", null,
    h("p", { class: "muted small", style: { margin: "4px 0 10px" } }, "Speech models for ffmpeg's built-in whisper. Each downloads once to this server, is checked against its published checksum, then runs offline on your GPU or CPU."),
    models,
    h("details", { style: { marginTop: "10px" } }, h("summary", { class: "muted small" }, "Use my own model file"),
      field("whisper.cpp model file", ffModel, "Full path to a ggml model on the server.")));
  const draw = () => {
    picks.replaceChildren(...[["none", "Off", "close"], ["server", "Speech server (GPU)", "bolt"], ["ffmpeg", "Built-in (ffmpeg whisper)", "memory"]]
      .map(([v, l, ic]) => h("button", { class: v === engine ? "sel" : "", onclick: () => { engine = v; draw(); } }, icon(ic, "sm"), l)));
    gServer.hidden = engine !== "server"; gFf.hidden = engine !== "ffmpeg";
  };
  draw();
  c.append(panel("Engine", null, picks, gServer, gFf, h("div", { class: "row wrap" }, field("Language", lang, "auto, en, es, de…"),
    h("div", { style: { paddingTop: "18px" } }, switchEl("Translate to English", translate, (v) => { translate = v; }))),
    h("button", { class: "btn primary", onclick: async () => {
      await api.put("/api/admin/stt", { engine, server_url: url.value, server_model: model.value, ffmpeg_model: ffModel.value, language: lang.value, translate });
      toast("Saved"); navigate("/admin/transcription", { replace: true });
    } }, "Save"),
    engine !== "none" ? h("div", { class: `callout ${st.ok ? "ok" : "bad"}`, style: { marginTop: "12px" } }, icon(st.ok ? "check" : "warning"),
      h("div", { class: "small" }, st.ok ? `Ready · ${st.queue} chunks queued${engine === "ffmpeg" ? ` · ${(st.models || []).find((m) => m.active)?.label || "custom model"}` : ""}` : st.error || "Not reachable")) : null));
}

// ---------------- printers ----------------
async function printers(c) {
  const list = await api.get("/api/printers");
  c.append(title("3D printers", "Send G-code straight from Paddock to printers on your network (Klipper/Moonraker or OctoPrint). Only local addresses are allowed."));
  const rows = list.map((p) => ({ ...p, api_key: "" }));
  const box = h("div");
  const draw = () => box.replaceChildren(...rows.map((p, i) => h("div", { class: "row wrap", style: { marginBottom: "8px" } },
    h("input", { class: "input", style: { maxWidth: "180px" }, value: p.name, placeholder: "Name", oninput: (e) => (p.name = e.target.value) }),
    h("select", { class: "input", style: { maxWidth: "150px" }, onchange: (e) => (p.type = e.target.value) }, ["moonraker", "octoprint"].map((t) => h("option", { value: t, selected: t === p.type }, t))),
    h("input", { class: "input", style: { maxWidth: "260px" }, value: p.url, placeholder: "http://192.168.1.60", oninput: (e) => (p.url = e.target.value) }),
    h("input", { class: "input", style: { maxWidth: "200px" }, type: "password", placeholder: "API key (optional)", oninput: (e) => (p.api_key = e.target.value) }),
    iconBtn("trash", "Remove", () => { rows.splice(i, 1); draw(); }))));
  draw();
  c.append(panel(null, null, box, h("div", { class: "row" },
    h("button", { class: "btn", onclick: () => { rows.push({ name: "", type: "moonraker", url: "", api_key: "" }); draw(); } }, icon("add", "sm"), "Add printer"),
    h("button", { class: "btn primary", onclick: async () => { await api.put("/api/admin/printers", rows); toast("Printers saved"); } }, "Save"))));
}

// ---------------- jobs ----------------
function jobsTable(list) {
  if (!list.length) return h("p", { class: "muted" }, "No jobs yet.");
  return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["Job", "Target", "Progress", "Message", "Started"].map((t) => h("th", null, t)))),
    h("tbody", null, list.map((j) => h("tr", null, h("td", null, j.kind), h("td", { class: "mono" }, j.target || ""),
      h("td", { style: { minWidth: "140px" } }, j.state === "running" ? meter(Math.round(j.progress * 100)) : h("span", { class: `pill ${j.state === "done" ? "ok" : j.state === "failed" ? "bad" : ""}` }, j.state)),
      h("td", { class: "small" }, j.message || ""), h("td", { class: "small muted" }, timeAgo(j.created_at)))))));
}

async function jobs(c) {
  c.append(title("Jobs", "Scans, thumbnails, transcodes, AI tagging, transcription, backups and offloads run in the background."));
  const box = h("div");
  c.append(box);
  const draw = async () => {
    const j = await api.get("/api/jobs", { quiet: true }).catch(() => null);
    if (j) box.replaceChildren(panel("Running", null, jobsTable(j.active)), panel("Recent", null, jobsTable(j.recent)));
  };
  await draw();
  timers.push(setInterval(draw, 2000));
}

// ---------------- security ----------------
async function security(c) {
  const [sessions, audit] = await Promise.all([api.get("/api/admin/sessions"), api.get("/api/admin/audit?limit=200")]);
  c.append(title("Security & audit", "Who is signed in, and everything that changed — by people, phones and AI agents."));
  c.append(panel("How Paddock protects you", null, h("ul", { class: "small muted", style: { margin: 0, paddingLeft: "18px" } },
    h("li", null, "Web requests are only accepted from your home network, the Paddock tunnel and linked servers — never the open internet."),
    h("li", null, "HTTPS everywhere with a private certificate authority; the app and linked servers pin it."),
    h("li", null, "Passwords are hashed with scrypt; tokens are stored only as SHA-256 hashes; repeated failed logins are slowed down exponentially."),
    h("li", null, "Each phone and server has its own revocable token and tunnel key; tunnels can reach Paddock and nothing else."),
    h("li", null, "AI agents get scoped tokens; they can never permanently delete; every change is in the log below."),
    h("li", null, "Backups are encrypted before they leave this server."))));
  c.append(panel("Active sessions", null, h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["User", "Kind", "Device", "IP", "Last seen", ""].map((t) => h("th", null, t)))),
    h("tbody", null, sessions.map((s) => h("tr", null, h("td", null, s.username), h("td", null, s.kind), h("td", null, s.device || "—"), h("td", { class: "mono" }, s.ip || ""),
      h("td", null, s.last_seen ? timeAgo(s.last_seen) : ""),
      h("td", null, s.kind === "web" ? h("button", { class: "btn ghost sm", onclick: async () => { await api.del(`/api/admin/sessions/${s.id}`); navigate("/admin/security", { replace: true }); } }, "End") : null))))))));
  c.append(panel("Audit log", null, h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["When", "Who", "What", "Details"].map((t) => h("th", null, t)))),
    h("tbody", null, audit.map((a) => h("tr", null, h("td", { class: "small" }, fmtDateTime(a.ts)), h("td", null, a.actor), h("td", null, h("code", null, a.action)),
      h("td", { class: "small muted", style: { maxWidth: "420px", wordBreak: "break-word" } }, JSON.stringify(a.detail).slice(0, 240)))))))));
}

// ---------------- general ----------------
async function general(c) {
  const net = await api.get("/api/admin/network");
  const name = h("input", { class: "input", value: ctx.server.name });
  const urls = h("textarea", { class: "input mono", placeholder: "https://192.168.1.50:8443" }, net.lan_urls.join("\n"));
  c.append(title("General"));
  c.append(panel("Server", null, field("Server name", name, "Shown in the app and to linked servers."),
    field("Home addresses given to phones", urls, "One per line. Paddock detects these automatically; override if your NAS has several interfaces."),
    h("button", { class: "btn primary", onclick: async () => {
      await api.put("/api/admin/general", { server_name: name.value, lan_urls: urls.value.split("\n").map((s) => s.trim()).filter(Boolean) });
      ctx.server.name = name.value; toast("Saved");
    } }, "Save")));
}
