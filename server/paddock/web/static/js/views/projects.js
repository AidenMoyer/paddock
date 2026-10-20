// Projects: gather footage, photos, sounds, CAD and notes; plan the video with AI.

import { api, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle } from "../app.js";
import { itemCard, projectCard, enableSelection } from "../cards.js";
import { renderMarkdown } from "../md.js";
import { shareDialog } from "./item.js";
import {
  h, icon, iconBtn, clear, emptyState, fmtDuration, fmtDate, timeAgo, toast, modal, field, confirmDialog, promptDialog, loader,
} from "../ui.js";

const STATUSES = ["idea", "active", "filming", "editing", "done"];
const ROLE_LABEL = { footage: "Footage", broll: "B-roll", photo: "Photos", audio: "Audio", music: "Music", cad: "CAD", print: "3D prints", reference: "Reference" };

export async function render(root, { match }) {
  if (match[1]) return detail(root, +match[1]);
  setTitle("Projects");
  root.append(h("div", { class: "page-head" }, h("h1", null, "Projects"),
    h("div", { class: "actions" }, h("button", { class: "btn accent", onclick: newProject }, icon("add"), "New project"))));
  const list = await api.get("/api/projects");
  if (!list.length) {
    root.append(emptyState("work", "Start your first project",
      "A project gathers everything about one build, trip or race weekend — footage, photos, sounds, CAD, notes and locations — and helps you plan a video about it.",
      h("button", { class: "btn accent", onclick: newProject }, icon("add"), "New project")));
    return;
  }
  for (const st of STATUSES) {
    const group = list.filter((p) => p.status === st);
    if (!group.length) continue;
    root.append(h("h2", { style: { margin: "16px 0 12px", textTransform: "capitalize" } }, st));
    root.append(h("div", { class: "grid" }, group.map(projectCard)));
  }
}

async function newProject() {
  const name = h("input", { class: "input", placeholder: "e.g. Turbo manifold build" });
  const goal = h("textarea", { class: "input", placeholder: "What should a video about this show? (optional)" });
  const loc = h("input", { class: "input", placeholder: "Shop, track, trail… (optional)" });
  const m = modal("New project", h("div", null, field("Name", name), field("Goal", goal), field("Location", loc)), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => {
      if (!name.value.trim()) return name.focus();
      const r = await api.post("/api/projects", { name: name.value, goal: goal.value, location_name: loc.value || null });
      m.close(); navigate(`/project/${r.id}`);
    } }, "Create")],
  });
}

async function detail(root, id) {
  const p = await api.get(`/api/projects/${id}`);
  setTitle(p.name);
  const statusSel = h("select", { class: "input", style: { width: "auto" }, onchange: async () => { await api.patch(`/api/projects/${id}`, { status: statusSel.value }); toast("Status updated"); } },
    STATUSES.map((s) => h("option", { value: s, selected: s === p.status }, s)));
  root.append(h("div", { class: "page-head" },
    h("div", { style: { flex: 1, minWidth: 0 } }, h("h1", null, p.name),
      h("div", { class: "muted" }, [p.location_name, `${p.items.length} items`, `${p.summary.footage_minutes} min footage`, `updated ${timeAgo(p.updated_at)}`].filter(Boolean).join(" · "))),
    h("div", { class: "actions" }, statusSel,
      h("button", { class: "btn", onclick: () => editProject(p) }, icon("edit"), "Edit"),
      h("button", { class: "btn", onclick: () => shareDialog(null, id) }, icon("share"), "Share CAD"),
      iconBtn("trash", "Delete project", async () => {
        if (await confirmDialog("Delete project?", "Only the project is deleted — its media stays in your libraries.", { ok: "Delete", danger: true })) {
          await api.del(`/api/projects/${id}`); navigate("/projects");
        }
      }))));
  if (p.goal) root.append(h("p", { class: "muted", style: { marginTop: "-8px" } }, h("b", null, "Goal: "), p.goal));

  const tabs = [["overview", "Overview"], ["media", "Media"], ["plan", "Video plan"], ["notes", `Notes (${p.notes.length})`], ["chat", "Ask AI"]];
  const tabBar = h("div", { class: "tabs" });
  const body = h("div");
  const show = (t) => {
    tabBar.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.t === t));
    history.replaceState({}, "", `${location.pathname}#${t}`);
    clear(body);
    ({ overview, media, plan, notes, chat })[t](body, p, id);
  };
  for (const [t, l] of tabs) tabBar.append(h("button", { "data-t": t, onclick: () => show(t) }, l));
  root.append(tabBar, body);
  show(location.hash.slice(1) || "overview");
}

function overview(body, p) {
  const s = p.summary;
  body.append(h("div", { class: "kpis" },
    kpi("Footage", `${s.footage_minutes} min`), ...Object.entries(s.counts_by_kind).map(([k, v]) => kpi(k.replace("_", " "), v)),
    kpi("Geotagged", s.geotagged), kpi("Shoot days", s.days.length)));
  if (!p.items.length) {
    body.append(emptyState("addCircle", "Add things to this project",
      "Open any video, photo, RED shoot day or CAD part and choose “Add to project”. Or ask the assistant: “add everything I shot last Saturday to this project”."));
    return;
  }
  body.append(h("h3", { style: { margin: "8px 0 12px" } }, "Timeline"));
  const byDay = {};
  for (const it of p.items) (byDay[it.date ? it.date.slice(0, 10) : "Undated"] ||= []).push(it);
  for (const [day, items] of Object.entries(byDay).sort()) {
    body.append(h("div", { class: "day-head" }, h("h3", null, day === "Undated" ? day : fmtDate(new Date(day).getTime() / 1000 + 43200, { weekday: "long", month: "long", day: "numeric", year: "numeric" })),
      h("span", { class: "muted small" }, `${items.length} items`)));
    body.append(h("div", { class: "grid dense" }, items.map((it) => itemCard(it, { sub: `${ROLE_LABEL[it.role] || it.role}${it.note ? " · " + it.note : ""}` }))));
  }
  if (s.centroid) body.append(h("p", { class: "muted small", style: { marginTop: "16px" } }, icon("place", "xs"), ` Centre of geotagged media: ${s.centroid[0].toFixed(4)}, ${s.centroid[1].toFixed(4)}`));
}

function kpi(l, v) { return h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v" }, v)); }

function media(body, p, id) {
  const roles = {};
  for (const it of p.items) (roles[it.role] ||= []).push(it);
  body.append(h("div", { class: "row wrap", style: { marginBottom: "16px" } },
    h("button", { class: "btn", onclick: async () => { await api.post(`/api/projects/${id}/autotag`); toast("AI is describing and tagging this project's media"); } }, icon("sparkle"), "AI tag everything"),
    h("button", { class: "btn", onclick: async () => {
      try { await api.post(`/api/projects/${id}/transcribe`, {}, { quiet: true }); toast("Transcribing all footage"); }
      catch (e) { toast(e.message, { error: true }); }
    } }, icon("cc"), "Transcribe footage")));
  for (const [role, items] of Object.entries(roles)) {
    body.append(h("h3", { style: { margin: "20px 0 12px" } }, `${ROLE_LABEL[role] || role} · ${items.length}`));
    const grid = h("div", { class: "grid dense" }, items.map((it) => itemCard(it, { sub: it.note || undefined })));
    body.append(grid);
    enableSelection(grid, null, [{ icon: "close", label: "Remove from project", fn: async (ids) => {
      await api.post(`/api/projects/${id}/items/remove`, { item_ids: ids }); toast("Removed"); navigate(location.pathname + "#media", { replace: true });
    } }]);
  }
}

function plan(body, p, id) {
  const view = h("div", { class: "md panel" });
  const draw = (md) => { view.innerHTML = md ? renderMarkdown(md) : ""; if (!md) view.append(emptyState("sparkle", "No plan yet", "Generate one from everything in this project, then edit it to taste.")); };
  const direction = h("input", { class: "input", placeholder: "Optional direction, e.g. “10-minute YouTube build video, upbeat, focus on fabrication”" });
  const gen = h("button", { class: "btn accent", onclick: async () => {
    gen.disabled = true;
    gen.replaceChildren(icon("sparkle"), "Planning…");
    view.replaceChildren(loader());
    try {
      const r = await api.post(`/api/projects/${id}/plan/generate`, { direction: direction.value });
      p.plan = r.plan;
      draw(r.plan);
      toast(r.provider === "template" ? "Made a starter plan (no AI model configured)" : "Plan ready");
    } catch { draw(p.plan); }
    gen.disabled = false;
    gen.replaceChildren(icon("sparkle"), "Generate plan");
  } }, icon("sparkle"), "Generate plan");
  const edit = h("button", { class: "btn", onclick: () => {
    const ta = h("textarea", { class: "input", style: { minHeight: "60vh", fontFamily: "var(--mono)", fontSize: "13px" } }, p.plan || "");
    const m = modal("Edit plan", ta, { wide: true, foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => {
      await api.put(`/api/projects/${id}/plan`, { plan: ta.value }); p.plan = ta.value; draw(ta.value); m.close(); toast("Plan saved");
    } }, "Save")] });
  } }, icon("edit"), "Edit");
  body.append(h("div", { class: "row wrap", style: { marginBottom: "16px" } }, direction, gen, edit,
    h("button", { class: "btn ghost", onclick: () => {
      const blob = new Blob([p.plan || ""], { type: "text/markdown" });
      const a = h("a", { href: URL.createObjectURL(blob), download: `${p.name} - video plan.md` });
      a.click();
    } }, icon("download"), "Export")));
  if (p.plan_updated_at) body.append(h("p", { class: "muted small" }, `Last updated ${timeAgo(p.plan_updated_at)}`));
  body.append(view);
  draw(p.plan);
}

function notes(body, p, id) {
  const ta = h("textarea", { class: "input", placeholder: "Ideas, measurements, shot ideas, to-dos…" });
  body.append(h("div", { class: "panel" }, ta, h("div", { class: "row", style: { marginTop: "8px", justifyContent: "flex-end" } },
    h("button", { class: "btn primary", onclick: async () => {
      if (!ta.value.trim()) return;
      await api.post(`/api/projects/${id}/notes`, { body: ta.value });
      p.notes.unshift({ body: ta.value, author: ctx.user.username, created_at: Date.now() / 1000 });
      clear(body); notes(body, p, id);
    } }, "Add note"))));
  for (const n of p.notes) {
    body.append(h("div", { class: "panel" }, h("div", { class: "muted small", style: { marginBottom: "6px" } }, `${n.author || ""} · ${timeAgo(n.created_at)}`),
      h("div", { class: "md", html: renderMarkdown(n.body) }),
      n.id ? h("div", { class: "row", style: { justifyContent: "flex-end" } }, h("button", { class: "btn ghost sm", onclick: async () => {
        await api.del(`/api/projects/${id}/notes/${n.id}`); p.notes = p.notes.filter((x) => x !== n); clear(body); notes(body, p, id);
      } }, "Delete")) : null));
  }
}

async function chat(body, p, id) {
  const { chatPanel } = await import("./assistant.js");
  body.append(chatPanel({ projectId: id, compact: true, suggestions: [
    "What footage am I missing to tell this story?",
    "Find clips from my other libraries that would work as b-roll for this project",
    "Write a 60-second voice-over for the intro",
    "Tag the best hook shots in this project",
  ] }));
}

function editProject(p) {
  const name = h("input", { class: "input", value: p.name });
  const desc = h("textarea", { class: "input" }, p.description || "");
  const goal = h("textarea", { class: "input" }, p.goal || "");
  const loc = h("input", { class: "input", value: p.location_name || "" });
  const m = modal("Edit project", h("div", null, field("Name", name), field("Description", desc), field("Goal", goal), field("Location", loc)), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn primary", onclick: async () => {
      await api.patch(`/api/projects/${p.id}`, { name: name.value, description: desc.value, goal: goal.value, location_name: loc.value });
      m.close(); navigate(location.pathname, { replace: true });
    } }, "Save")],
  });
}
