// Your account: profile, security, devices (QR pairing), share links, trash.

import { api, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle } from "../app.js";
import { itemCard } from "../cards.js";
import {
  h, icon, iconBtn, clear, emptyState, timeAgo, fmtDateTime, toast, modal, field, confirmDialog, switchEl, copyText,
} from "../ui.js";

export async function render(root, { path }) {
  if (path === "/trash") return trash(root);
  setTitle("Your account");
  root.append(h("div", { class: "page-head" }, h("h1", null, `Hi, ${ctx.user.display_name || ctx.user.username}`)));
  const grid = h("div", { class: "panels" });
  root.append(grid);

  // profile & preferences
  const name = h("input", { class: "input", value: ctx.user.display_name || "" });
  grid.append(h("section", { class: "panel" }, h("h3", null, "Profile"), h("p", { class: "desc" }, `Username: ${ctx.user.username}${ctx.user.is_admin ? " · administrator" : ""}`),
    field("Display name", name),
    h("button", { class: "btn primary", onclick: async () => { ctx.user = await api.patch("/api/me", { display_name: name.value }); toast("Saved"); } }, "Save"),
    h("hr", { style: { border: 0, borderTop: "1px solid var(--border)", margin: "16px 0" } }),
    switchEl("Let the assistant make changes", ctx.user.allow_ai_changes, async (on) => { ctx.user = await api.patch("/api/me", { allow_ai_changes: on }); },
      "Tags, collections, projects and plans. It can never permanently delete anything."),
    switchEl("Autoplay next video", localStorage.getItem("autoplay") !== "0", (on) => localStorage.setItem("autoplay", on ? "1" : "0")),
    h("div", { class: "field", style: { marginTop: "8px" } }, h("label", null, "Theme"),
      h("select", { class: "input", onchange: (e) => { const v = e.target.value; if (v === "system") { delete document.documentElement.dataset.theme; localStorage.removeItem("theme"); } else { document.documentElement.dataset.theme = v; localStorage.setItem("theme", v); } } },
        ...[["system", "Match system"], ["dark", "Dark"], ["light", "Light"]].map(([v, l]) => h("option", { value: v, selected: (localStorage.getItem("theme") || "system") === v }, l))))));

  // password
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password" });
  const nw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
  grid.append(h("section", { class: "panel" }, h("h3", null, "Password"), h("p", { class: "desc" }, "Changing it signs out your other browser sessions. Paired phones stay connected."),
    field("Current password", cur), field("New password", nw, "At least 10 characters."),
    h("button", { class: "btn primary", onclick: async () => { await api.post("/api/auth/password", { current: cur.value, new: nw.value }); cur.value = nw.value = ""; toast("Password changed"); } }, "Change password")));

  // devices
  const devPanel = h("section", { class: "panel", id: "devices", style: { gridColumn: "1 / -1" } });
  grid.append(devPanel);
  await drawDevices(devPanel);

  // share links
  const sharePanel = h("section", { class: "panel", style: { gridColumn: "1 / -1" } });
  grid.append(sharePanel);
  drawShares(sharePanel);
  if (location.hash === "#devices") setTimeout(() => devPanel.scrollIntoView({ behavior: "smooth" }), 100);
}

async function drawDevices(panel) {
  const devices = await api.get("/api/devices");
  clear(panel).append(h("div", { class: "row" }, h("h3", { style: { flex: 1 } }, "Phones & tablets"),
    h("button", { class: "btn accent", onclick: () => pairDialog(() => drawDevices(panel)) }, icon("phone"), "Connect a phone")),
    h("p", { class: "desc" }, "Paired devices stream and back up at home over Wi-Fi, and anywhere else through their own private tunnel that can only reach Paddock."));
  const active = devices.filter((d) => !d.revoked_at);
  if (!active.length) { panel.append(h("p", { class: "muted" }, "No devices yet.")); return; }
  panel.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["Device", "Owner", "Tunnel address", "Paired", "Last seen", ""].map((t) => h("th", null, t)))),
    h("tbody", null, active.map((d) => h("tr", null,
      h("td", null, h("div", { class: "row" }, icon("phone", "sm"), d.name, h("span", { class: "pill" }, d.platform))),
      h("td", null, d.username), h("td", { class: "mono" }, d.wg_ip || "—"), h("td", null, timeAgo(d.created_at)),
      h("td", null, d.last_seen ? timeAgo(d.last_seen) : "never"),
      h("td", null, h("button", { class: "btn danger sm", onclick: async () => {
        if (await confirmDialog(`Disconnect ${d.name}?`, "Its access token and tunnel key stop working immediately. You can pair it again later.", { ok: "Disconnect", danger: true })) {
          await api.del(`/api/devices/${d.id}`); drawDevices(panel);
        }
      } }, "Disconnect"))))))));
}

export async function pairDialog(onDone) {
  const r = await api.post("/api/devices/pairing");
  const p = r.payload;
  const qr = h("div", { class: "qr", html: r.qr_svg });
  const left = h("span");
  const tick = () => {
    const s = Math.max(0, Math.round(r.expires_at - Date.now() / 1000));
    left.textContent = s ? `Code expires in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "Code expired — close and try again";
  };
  tick();
  const timer = setInterval(tick, 1000);
  modal("Connect a phone", h("div", { class: "stack", style: { alignItems: "center", textAlign: "center" } },
    h("ol", { style: { textAlign: "left", margin: 0, paddingLeft: "20px" } },
      h("li", null, "Install the Paddock app on your Android phone — in the phone's browser open ",
        h("b", { style: { wordBreak: "break-all" } }, `${(p.lan && p.lan[0]) || location.origin}/download/android`), "."),
      h("li", null, "Make sure the phone is on your home Wi-Fi."),
      h("li", null, "Open the app, tap ", h("b", null, "Scan pairing code"), ", and point it at this code.")),
    qr, h("div", { class: "muted small" }, left),
    h("div", { class: "callout", style: { textAlign: "left", width: "100%" } }, icon("shield"),
      h("div", { class: "small" }, "The code works once, for 10 minutes, and only on your home network. It also carries this server's certificate fingerprint so the app can't be tricked into talking to anything else.",
        p.wg ? " Remote access: on." : " Remote access isn't configured yet — the phone will work at home only until an admin enables it in Manage server → Remote access."))),
  { onclose: () => { clearInterval(timer); onDone?.(); } });
}

async function drawShares(panel) {
  const links = await api.get("/api/share");
  clear(panel).append(h("h3", null, "Share links"), h("p", { class: "desc" }, "Expiring download links you've created. They only work on your network or through a Paddock tunnel."));
  if (!links.length) { panel.append(h("p", { class: "muted" }, "None yet — use “Share” on any item or project.")); return; }
  panel.append(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["What", "Created", "Expires", "Downloads", ""].map((t) => h("th", null, t)))),
    h("tbody", null, links.map((l) => h("tr", null,
      h("td", null, l.item_title || l.project_name || "—"), h("td", null, timeAgo(l.created_at)),
      h("td", null, l.expires_at < Date.now() / 1000 ? h("span", { class: "pill bad" }, "expired") : fmtDateTime(l.expires_at)),
      h("td", null, `${l.downloads}${l.max_downloads ? ` / ${l.max_downloads}` : ""}`),
      h("td", null, h("button", { class: "btn ghost sm", onclick: async () => { await api.del(`/api/share/${l.id}`); drawShares(panel); } }, "Revoke"))))))));
}

async function trash(root) {
  setTitle("Trash");
  const items = await api.get("/api/trash");
  root.append(h("div", { class: "page-head" }, h("h1", null, "Trash"),
    h("div", { class: "actions" },
      items.length ? h("button", { class: "btn", onclick: async () => { await api.post("/api/items/restore", { ids: items.map((i) => i.id) }); navigate("/trash", { replace: true }); } }, icon("restore"), "Restore all") : null,
      items.length && ctx.user.is_admin ? h("button", { class: "btn danger", onclick: async () => {
        const typed = await confirmDialog("Permanently delete files?", `${items.length} files will be deleted from the NAS. If your dataset has ZFS snapshots you can still recover them from TrueNAS.`, { ok: "Delete forever", danger: true, typed: "DELETE" });
        if (!typed) return;
        const r = await api.post("/api/trash/empty", { ids: items.map((i) => i.id), confirm: typed });
        toast(`Deleted ${r.deleted} files${r.failed.length ? `, ${r.failed.length} failed (read-only dataset?)` : ""}`);
        navigate("/trash", { replace: true });
      } }, icon("trash"), "Empty trash") : null)));
  root.append(h("p", { class: "muted" }, "Items in the trash are hidden everywhere in Paddock. Files stay on disk until an admin empties the trash. AI agents can move things here but can never empty it."));
  if (!items.length) return root.append(emptyState("trash", "Trash is empty"));
  const grid = h("div", { class: "grid dense" });
  for (const it of items) {
    const card = itemCard(it, { href: "#" });
    card.addEventListener("click", async (e) => {
      e.preventDefault();
      await api.post("/api/items/restore", { ids: [it.id] });
      card.remove();
      toast(`Restored ${it.title}`);
    });
    grid.append(card);
  }
  root.append(h("p", { class: "small faint" }, "Click an item to restore it."), grid);
}
