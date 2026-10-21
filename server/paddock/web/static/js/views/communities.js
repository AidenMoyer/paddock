// Manage server → Communities, plus the people and library settings that go with them.

import { api } from "../api.js";
import { ctx, navigate, refreshLibraries } from "../app.js";
import { h, icon, toast, modal, field, switchEl, confirmDialog, timeAgo, emptyState } from "../ui.js";

const title = (t, d, ...actions) => h("div", null, h("div", { class: "page-head", style: { marginTop: 0 } }, h("h1", null, t), h("div", { class: "actions" }, ...actions)), d ? h("p", { class: "muted", style: { marginTop: "-8px" } }, d) : null);
const panel = (t, d, ...kids) => h("section", { class: "panel" }, t ? h("h3", null, t) : null, d ? h("p", { class: "desc" }, d) : null, ...kids);
const FEATURES = [["comments", "Comments", "Members can comment on videos and photos for each other."],
  ["parties", "Watch parties", "Members can start watch parties everyone in the community can join."],
  ["voice", "Voice lounge", "An always-open voice room for members, and calls between them."]];

export async function communitiesPage(c) {
  const [list, users, libs] = await Promise.all([api.get("/api/communities"), api.get("/api/users"), api.get("/api/libraries")]);
  c.append(title("Communities", "Groups of people: a household, the race team, friends. Share libraries and projects with a community (read-only or with changes allowed), and give it comments, watch parties and a voice lounge.",
    h("button", { class: "btn accent", onclick: () => edit(null, users) }, icon("add"), "New community")));
  if (!list.length) {
    c.append(emptyState("users", "No communities yet", "Without communities everyone on this server sees every library, like before. Create one to share some libraries with only some people."));
    return;
  }
  for (const cm of list) {
    c.append(panel(null, null,
      h("div", { class: "row wrap" }, h("div", { class: "badge-kind" }, icon("users")),
        h("div", { style: { flex: 1, minWidth: "200px" } }, h("h3", null, cm.name), cm.description ? h("div", { class: "muted small" }, cm.description) : null,
          h("div", { class: "muted small" }, FEATURES.filter(([k]) => cm.settings[k]).map(([, l]) => l).join(" · ") || "Sharing only")),
        h("button", { class: "btn sm", onclick: () => edit(cm, users) }, "Edit"),
        h("button", { class: "btn sm danger", onclick: async () => {
          if (await confirmDialog(`Delete ${cm.name}?`, "Its members lose access to the libraries and projects shared only with it. Comments made in it are deleted.", { ok: "Delete", danger: true })) {
            await api.del(`/api/communities/${cm.id}`); navigate("/admin/communities", { replace: true });
          }
        } }, "Delete")),
      h("div", { class: "row wrap", style: { marginTop: "10px", gap: "6px" } }, ...cm.members.map((m) =>
        h("span", { class: "pill" }, m.display_name || m.username, m.role === "manager" ? " · manager" : ""))),
      h("div", { class: "muted small", style: { marginTop: "10px" } },
        cm.libraries.length ? `Libraries: ${cm.libraries.map((l) => `${l.name} (${l.access === "write" ? "can change" : "view"})`).join(", ")}` : "No libraries shared only with this community",
        cm.projects.length ? ` · Projects: ${cm.projects.map((p) => p.name).join(", ")}` : "")));
  }
}

function edit(cm, users) {
  const name = h("input", { class: "input", value: cm?.name || "", maxlength: 60, placeholder: "Family" });
  const desc = h("input", { class: "input", value: cm?.description || "", maxlength: 500 });
  const settings = { comments: true, parties: true, voice: true, ...(cm?.settings || {}) };
  const roles = new Map((cm?.members || []).map((m) => [m.user_id, m.role]));
  const people = h("div", { class: "people-pick" }, users.map((u) => {
    const role = h("select", { class: "input", style: { width: "auto" }, disabled: !roles.has(u.id), onchange: (e) => roles.set(u.id, e.target.value) },
      h("option", { value: "member", selected: roles.get(u.id) !== "manager" }, "Member"), h("option", { value: "manager", selected: roles.get(u.id) === "manager" }, "Manager"));
    return h("label", { class: "row small" }, h("input", { type: "checkbox", checked: roles.has(u.id), onchange: (e) => {
      if (e.target.checked) roles.set(u.id, role.value); else roles.delete(u.id);
      role.disabled = !e.target.checked;
    } }), h("span", { style: { flex: 1 } }, u.display_name || u.username), role);
  }));
  const m = modal(cm ? `Edit ${cm.name}` : "New community", h("div", { class: "stack" },
    field("Name", name), field("Description", desc),
    field("People", people, "Managers can add and remove members."),
    ...FEATURES.map(([k, l, d]) => switchEl(l, settings[k], (v) => { settings[k] = v; }, d))), {
    wide: true,
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      let id = cm?.id;
      if (!id) id = (await api.post("/api/communities", { name: name.value, description: desc.value, settings })).id;
      else await api.patch(`/api/communities/${id}`, { name: name.value, description: desc.value, settings });
      await api.put(`/api/communities/${id}/members`, { members: [...roles].map(([user_id, role]) => ({ user_id, role })) });
      m.close(); toast("Saved"); navigate("/admin/communities", { replace: true });
    } }, cm ? "Save" : "Create")],
  });
}

/** Who can see a library: everyone, or only chosen communities (each read-only or allowed to change things). */
export async function libraryAccessDialog(lib) {
  const communities = await api.get("/api/communities");
  let visibility = lib.options?.visibility || "everyone";
  const grants = new Map((lib.communities || []).map((g) => [g.id, g.access]));
  const rows = h("div", { class: "people-pick" }, communities.map((cm) => {
    const level = h("select", { class: "input", style: { width: "auto" }, disabled: !grants.has(cm.id), onchange: (e) => grants.set(cm.id, e.target.value) },
      h("option", { value: "read", selected: grants.get(cm.id) !== "write" }, "Can view"), h("option", { value: "write", selected: grants.get(cm.id) === "write" }, "Can change"));
    return h("label", { class: "row small" }, h("input", { type: "checkbox", checked: grants.has(cm.id), onchange: (e) => {
      if (e.target.checked) grants.set(cm.id, level.value); else grants.delete(cm.id);
      level.disabled = !e.target.checked;
    } }), h("span", { style: { flex: 1 } }, cm.name), level);
  }));
  const pick = h("div", { class: "kind-pick" });
  const drawPick = () => {
    pick.replaceChildren(...[["everyone", "Everyone on this server", "globe"], ["communities", "Only some communities", "users"]].map(([v, l, ic]) =>
      h("button", { class: v === visibility ? "sel" : "", onclick: () => { visibility = v; drawPick(); } }, icon(ic, "sm"), l)));
    rows.hidden = visibility !== "communities";
  };
  drawPick();
  const m = modal(`Who can see ${lib.name}`, h("div", { class: "stack" }, pick,
    communities.length ? rows : h("p", { class: "muted small" }, "Create a community first (Manage server → Communities)."),
    h("p", { class: "muted small" }, "Admins always see every library. People in media-only mode only ever see movies, shows, music, photos and home videos.")), {
    wide: true,
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      await api.put(`/api/libraries/${lib.id}/access`, { visibility, communities: [...grants].map(([id, access]) => ({ id, access })) });
      m.close(); toast("Saved"); await refreshLibraries(); navigate("/admin/libraries", { replace: true });
    } }, "Save")],
  });
}

/** One person's settings: admin, media-only, and where their phone backups go. */
export function userDialog(u, libs) {
  let admin = u.is_admin, mediaOnly = u.media_only_self;
  const lib = h("select", { class: "input" }, h("option", { value: 0 }, "The server's default"),
    libs.filter((l) => ["photos", "home_videos", "mixed"].includes(l.kind)).map((l) => h("option", { value: l.id, selected: l.id === u.upload_library_id }, l.name)));
  const folder = h("input", { class: "input", value: u.upload_folder || "", placeholder: `e.g. Family/${u.username}`, maxlength: 200 });
  const m = modal(u.display_name || u.username, h("div", { class: "stack" },
    switchEl("Administrator", admin, (v) => { admin = v; }, "Can manage libraries, people, devices and settings, and sees everything."),
    switchEl("Media-only", mediaOnly, (v) => { mediaOnly = v; }, "Only movies, shows, music, photos and home videos — no engineering files, footage, projects or assistant. Watch parties, calls and comments still work."),
    field("Phone backups go to", lib), field("In this folder", folder, "Leave empty for Phone Backups/<phone name>.")), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      await api.patch(`/api/users/${u.id}`, { is_admin: admin, media_only: mediaOnly, upload_library_id: +lib.value, upload_folder: folder.value });
      m.close(); toast("Saved"); navigate("/admin/users", { replace: true });
    } }, "Save")],
  });
}
