// Detail page for photos, CAD, 3D models, G-code, PCB and documents.

import { api, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle, itemHref } from "../app.js";
import { addToProjectDialog, itemCard } from "../cards.js";
import { lightbox } from "./photos.js";
import { gcodeView, viewer3d } from "../viewer3d.js";
import { h, icon, fmtBytes, fmtDateTime, timeAgo, toast, modal, field, copyText, promptDialog } from "../ui.js";

const LABELS = {
  format: "Format", category: "Category", revision: "Revision", size_x: "Width (X)", size_y: "Depth (Y)", size_z: "Height (Z)",
  triangles: "Triangles", volume_cm3: "Volume", surface_cm2: "Surface", print_time: "Print time", filament_g: "Filament",
  filament_mm: "Filament length", filament_type: "Material", layer_height: "Layer height", nozzle: "Nozzle", printer: "Printer",
  infill: "Infill", slicer: "Slicer", designer: "Designer", title: "Model title", application: "Made with", schema: "STEP schema",
  originating_system: "Exported from", author: "Author", organization: "Organization", step_timestamp: "Exported", label: "Document",
  kicad_title: "Board", kicad_rev: "Board rev", kicad_company: "Company", copper_layers: "Copper layers", freecad_version: "FreeCAD",
  make: "Camera make", model: "Camera", lens: "Lens", iso: "ISO", f_number: "Aperture", exposure: "Shutter", focal_length: "Focal length",
};
const UNITS = { size_x: " mm", size_y: " mm", size_z: " mm", volume_cm3: " cm³", surface_cm2: " cm²", filament_g: " g", filament_mm: " mm", layer_height: " mm" };

export async function render(root, { match }) {
  const id = +match[1];
  const item = await api.get(`/api/items/${id}`);
  setTitle(item.title);
  const m = item.meta || {};
  const left = h("div", { class: "primary" });
  const right = h("div", { class: "secondary" });
  root.append(h("div", { class: "watch" }, left, right));

  // ---- visual
  const ext = item.ext;
  if (item.kind === "photo") {
    const img = h("img", { src: `/api/items/${id}/preview`, alt: item.title, style: { width: "100%", borderRadius: "12px", cursor: "zoom-in", background: "var(--surface)" } });
    img.onclick = () => lightbox([item], 0);
    left.append(img);
  } else if ([".stl", ".obj", ".3mf"].includes(ext)) {
    left.append(viewer3d(`/api/items/${id}/mesh.stl`));
  } else if (item.kind === "gcode" && ext !== ".bgcode") {
    const tabs = h("div", { class: "chips" });
    const stage = h("div");
    const showPreview = () => stage.replaceChildren(gcodeView(`/api/items/${id}/gcode`));
    const showThumb = () => stage.replaceChildren(h("div", { class: "thumb contain" }, h("img", { src: thumbUrl(id), alt: "" })));
    tabs.append(h("button", { class: "chip active", onclick: (e) => { sel(e); showPreview(); } }, "Toolpath"),
      item.has_thumb ? h("button", { class: "chip", onclick: (e) => { sel(e); showThumb(); } }, "Slicer preview") : null);
    const sel = (e) => { tabs.querySelectorAll(".chip").forEach((c) => c.classList.remove("active")); e.target.classList.add("active"); };
    left.append(stage, tabs);
    showPreview();
  } else if (ext === ".pdf") {
    left.append(h("iframe", { src: `/api/items/${id}/file`, title: item.title, style: { width: "100%", height: "75vh", border: 0, borderRadius: "12px", background: "#fff" } }));
  } else if (item.has_thumb) {
    left.append(h("div", { class: "thumb contain" }, h("img", { src: thumbUrl(id), alt: "" })));
  } else {
    left.append(h("div", { class: "thumb" }, h("div", { class: "fallback" }, icon("doc"), m.format || ext)));
  }

  // ---- title & actions
  left.append(h("h1", { class: "watch-title" }, item.title));
  const printers = item.kind === "gcode" ? await api.get("/api/printers").catch(() => []) : [];
  left.append(h("div", { class: "watch-actions" },
    h("a", { class: "btn primary", href: `/api/items/${id}/file?download=1`, download: "" }, icon("download"), "Download"),
    printers.length ? h("button", { class: "btn accent", onclick: () => printDialog(id, printers) }, icon("printer"), "Send to printer") : null,
    h("button", { class: "btn", onclick: () => addToProjectDialog([id]) }, icon("work"), "Add to project"),
    h("button", { class: "btn", onclick: () => shareDialog(id) }, icon("share"), "Share link"),
    h("button", { class: "btn", onclick: async () => {
      const t = await promptDialog("Add tag", { placeholder: "e.g. jig, bracket, PETG" });
      if (t) { await api.patch(`/api/items/${id}`, { add_tags: [t] }); toast("Tagged"); navigate(location.pathname, { replace: true }); }
    } }, icon("tag"), "Tag")));

  // ---- facts
  const rows = [];
  for (const [k, label] of Object.entries(LABELS)) {
    if (m[k] === undefined || m[k] === null || m[k] === "") continue;
    let v = m[k];
    if (typeof v === "number") v = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(2);
    rows.push([label, `${v}${UNITS[k] || ""}`]);
  }
  rows.push(["File", item.path], ["Size", fmtBytes(item.size)], ["Modified", fmtDateTime(item.taken_at)], ["Added", timeAgo(item.added_at)], ["Library", item.library]);
  if (m.parameters?.length) rows.push(["Parameters", m.parameters.join(", ")]);
  if (m.parse_error) rows.push(["Note", `Couldn't fully read this file: ${m.parse_error}`]);
  left.append(h("div", { class: "desc-box" },
    h("dl", { class: "kv" }, ...rows.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
    item.tags?.length ? h("div", { class: "tags", style: { marginTop: "12px" } }, ...item.tags.map((t) => h("a", { class: "tag", href: `/search?tag=${encodeURIComponent(t)}` }, `#${t}`))) : null,
    item.projects?.length ? h("div", { class: "row wrap", style: { marginTop: "12px" } }, h("span", { class: "muted small" }, "Projects:"),
      ...item.projects.map((p) => h("a", { class: "pill", href: `/project/${p.id}` }, p.name))) : null));

  // ---- related (revisions / exports / same day)
  const related = (item.related || []).filter((r) => r.id !== id);
  if (related.length) {
    right.append(h("h3", { style: { marginBottom: "12px" } }, item.group?.startsWith("eng:") ? "Other revisions & exports" : "More from this group"));
    const list = h("div", { class: "upnext" });
    for (const r of related.slice(0, 30)) list.append(itemCard(r, { badge: false }));
    right.append(list);
  }
}

function printDialog(id, printers) {
  const sel = h("select", { class: "input" }, printers.map((p) => h("option", { value: p.name }, `${p.name} (${p.type})`)));
  const start = h("input", { type: "checkbox" });
  const m = modal("Send to printer", h("div", null, field("Printer", sel),
    h("label", { class: "row" }, start, "Start printing immediately"),
    h("p", { class: "muted small" }, "The file is uploaded over your home network only. Make sure the bed is clear before starting a print remotely.")), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      const r = await api.post(`/api/items/${id}/print`, { printer: sel.value, start: start.checked });
      m.close(); toast(`Sent ${r.file} to ${r.printer}${r.started ? " — printing" : ""}`);
    } }, icon("printer"), "Send")],
  });
}

export function shareDialog(id, projectId = null) {
  const hours = h("select", { class: "input" }, [[1, "1 hour"], [24, "1 day"], [72, "3 days"], [168, "1 week"], [720, "30 days"]].map(([v, l]) => h("option", { value: v, selected: v === 72 }, l)));
  const cap = h("input", { class: "input", type: "number", min: 1, placeholder: "Unlimited" });
  const out = h("div");
  const m = modal("Share link", h("div", null,
    h("p", { class: "muted", style: { marginTop: 0 } }, "Anyone with the link can download — but only from your home network or through a Paddock tunnel. Nothing is published to the internet."),
    field("Expires after", hours), field("Max downloads", cap), out), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Close"), h("button", { class: "btn primary", onclick: async () => {
      const r = await api.post("/api/share", { item_id: projectId ? null : id, project_id: projectId, hours: +hours.value, max_downloads: cap.value ? +cap.value : null });
      out.replaceChildren(h("div", { class: "callout ok" }, icon("link"), h("div", { style: { minWidth: 0 } }, h("code", null, r.url),
        h("div", null, h("button", { class: "btn link sm", onclick: () => copyText(r.url) }, "Copy")))));
      copyText(r.url);
    } }, "Create link")],
  });
}
