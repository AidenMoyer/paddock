// Upload page + global upload queue (also fed by drag & drop anywhere).

import { uploadFile } from "../api.js";
import { ctx, setTitle } from "../app.js";
import { h, icon, fmtBytes, toast } from "../ui.js";

const queue = [];
let running = false;
let listEl = null;

export function queueUploads(files, opts = {}) {
  for (const f of files) {
    const row = { file: f, opts, progress: 0, state: "Waiting", el: null };
    queue.push(row);
  }
  draw();
  pump();
}

async function pump() {
  if (running) return;
  running = true;
  while (true) {
    const next = queue.find((r) => r.state === "Waiting");
    if (!next) break;
    next.state = "Starting";
    try {
      const r = await uploadFile(next.file, { ...next.opts, onProgress: (p, s) => { next.progress = p; next.state = s; drawRow(next); } });
      next.state = r.duplicate ? "Already in Paddock" : "Done";
      next.progress = 1;
    } catch (e) {
      next.state = `Failed: ${e.message}`;
    }
    drawRow(next);
  }
  running = false;
  const done = queue.filter((r) => r.state === "Done").length;
  if (done) toast(`${done} file${done > 1 ? "s" : ""} uploaded`);
}

function drawRow(r) {
  if (!r.el) return;
  const fill = r.el.querySelector(".meter i");
  fill.style.width = `${Math.round(r.progress * 100)}%`;
  r.el.querySelector(".st").textContent = r.state;
}

function draw() {
  if (!listEl) return;
  listEl.replaceChildren(...queue.slice().reverse().map((r) => {
    r.el = h("div", { class: "upload-row" }, icon(r.file.type.startsWith("video") ? "videocam" : r.file.type.startsWith("image") ? "photo" : "doc"),
      h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, r.file.name),
        h("div", { class: "meter" }, h("i", { style: { width: `${r.progress * 100}%` } }))),
      h("span", { class: "muted small", style: { minWidth: "90px" } }, fmtBytes(r.file.size)), h("span", { class: "st muted small", style: { minWidth: "120px" } }, r.state));
    return r.el;
  }));
}

export async function render(root, { query }) {
  setTitle("Upload");
  const target = query.get("to");
  const libs = ctx.libraries.filter((l) => (target ? l.kind === target : ["photos", "home_videos", "engineering", "mixed", "red"].includes(l.kind)));
  const lib = h("select", { class: "input", style: { maxWidth: "320px" } },
    h("option", { value: "" }, "Automatic (phone backup library)"), libs.map((l) => h("option", { value: l.id, selected: target && l.kind === target }, `${l.name} (${l.kind_label})`)));
  const sub = h("input", { class: "input", style: { maxWidth: "320px" }, placeholder: "Optional folder name, e.g. Dyno Day" });
  const pick = h("input", { type: "file", multiple: true, hidden: true });
  pick.addEventListener("change", () => { queueUploads([...pick.files], { libraryId: lib.value ? +lib.value : null, subfolder: sub.value }); pick.value = ""; });
  const drop = h("div", { class: "dropzone", onclick: () => pick.click() }, icon("upload"),
    h("h2", { style: { margin: "12px 0 4px", color: "var(--text)" } }, "Drag files here or click to choose"),
    h("p", { style: { margin: 0 } }, "Photos, videos, music, STL/3MF/G-code, STEP and more. Uploads resume if your connection drops, and duplicates are skipped automatically."));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault(); e.stopPropagation(); drop.classList.remove("over");
    document.body.classList.remove("dragging");
    queueUploads([...e.dataTransfer.files], { libraryId: lib.value ? +lib.value : null, subfolder: sub.value });
  });
  listEl = h("div", { style: { marginTop: "20px" } });
  root.append(h("div", { class: "page-head" }, h("h1", null, "Upload")),
    h("div", { class: "row wrap", style: { marginBottom: "16px" } }, lib, sub), drop, pick, listEl,
    h("div", { class: "callout", style: { marginTop: "24px" } }, icon("phone"),
      h("div", null, h("b", null, "Backing up a phone? "), "Install the Paddock Android app and pair it from ", h("a", { href: "/account#devices", style: { color: "var(--link)" } }, "Your account → Devices"),
        ". It backs up new photos and videos automatically — at home over Wi-Fi, or anywhere through its private tunnel.")));
  draw();
  return { destroy: () => { listEl = null; } };
}
