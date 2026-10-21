// Public links: one video, its own password, watchable from anywhere on the admin's own domain.

import { api } from "../api.js";
import { navigate } from "../app.js";
import { h, icon, iconBtn, toast, modal, field, switchEl, copyText, confirmDialog, fmtDateTime, timeAgo, loader } from "../ui.js";

const title = (t, d, ...actions) => h("div", null, h("div", { class: "page-head", style: { marginTop: 0 } }, h("h1", null, t), h("div", { class: "actions" }, ...actions)), d ? h("p", { class: "muted", style: { marginTop: "-8px" } }, d) : null);
const panel = (t, d, ...kids) => h("section", { class: "panel" }, t ? h("h3", null, t) : null, d ? h("p", { class: "desc" }, d) : null, ...kids);
const AGREEMENT = "https://letsencrypt.org/repository/";
const DURATIONS = [[1, "1 day"], [7, "7 days"], [30, "30 days"], [0, "Until I turn it off"]];

/** An easy-to-type password: 12 characters without look-alikes (about 59 bits). */
export function newPassword() {
  const abc = "abcdefghjkmnpqrstuvwxyz23456789";
  let s = "";
  while (s.length < 12) {
    for (const b of crypto.getRandomValues(new Uint8Array(16))) if (b < 248 && s.length < 12) s += abc[b % abc.length];
  }
  return `${s.slice(0, 4)}-${s.slice(4, 8)}-${s.slice(8)}`;
}

const statePill = (l) => ({
  active: h("span", { class: "pill ok" }, "On"),
  expired: h("span", { class: "pill" }, "Expired"),
  off: h("span", { class: "pill" }, "Turned off"),
  locked: h("span", { class: "pill bad", title: "Too many wrong passwords from different places" }, `Locked until ${fmtDateTime(l.locked_until)}`),
}[l.state]);

const check = (ok, text, detail) => h("div", { class: "row", style: { alignItems: "flex-start", gap: "10px", margin: "10px 0" } },
  icon(ok === true ? "check" : ok === "busy" ? "refresh" : "warning", "sm"),
  h("div", null, h("div", { style: { fontWeight: 500 } }, text), detail ? h("div", { class: "muted small" }, detail) : null));

export async function publicPage(c, timers) {
  c.append(title("Public links", "Let people outside your home watch one video in their browser, with no account or app. Each link has its own password and expiry, and you can turn it off any time. The rest of Paddock stays private."));
  const setup = h("div", null, loader());
  const list = h("div", null, loader());
  c.append(panel("Your domain", null, setup), panel("Links", "Create a link from any video's page with Share online.", list));
  let busyTimer = null, first = true;
  const redraw = async () => {
    clearTimeout(busyTimer);
    const st = await api.get("/api/admin/public");
    if (!first && !setup.isConnected) return;     // left the page
    first = false;
    drawSetup(setup, st, redraw);
    if (st.certificate_busy) busyTimer = setTimeout(redraw, 4000);
  };
  timers.push(setInterval(() => { if (list.isConnected) drawLinks(list); }, 15000));
  await Promise.all([redraw(), drawLinks(list, true)]);
}

function drawSetup(box, st, redraw) {
  const s = st.settings;
  const domain = h("input", { class: "input", value: s.domain, placeholder: "videos.example.com", autocapitalize: "off", spellcheck: false });
  const email = h("input", { class: "input", type: "email", value: s.email, placeholder: "Optional: you@example.com" });
  const viewers = h("input", { class: "input", type: "number", min: 1, max: 50, value: s.max_viewers, style: { maxWidth: "120px" } });
  let auto = s.auto_portmap;
  const agreed = h("input", { type: "checkbox", checked: s.agreed });
  const save = async (enabled) => {
    const r = await api.put("/api/admin/public", { enabled, domain: domain.value.trim(), email: email.value.trim(), agreed: agreed.checked, auto_portmap: auto, max_viewers: +viewers.value || 4 });
    toast(!enabled ? "Public links are off" : r.job_id ? "Saved. Getting a certificate from Let's Encrypt…" : "Saved");
    redraw();
  };
  const ip = st.public_ip || "your home's public address";
  const cert = st.certificate;
  const status = !s.enabled ? null : h("div", { class: "public-status" },
    st.cgnat ? h("div", { class: "callout bad" }, icon("warning"), h("div", { class: "small" },
      "Your internet provider shares one public address between many homes (CGNAT), so nobody outside can reach this server directly. Public links can't work on this connection; ask your provider for a public IPv4 address.")) : null,
    check(st.dns_ok, st.dns_ok ? `${s.domain} points to your home (${st.public_ip})` : st.dns.length ? `${s.domain} points to ${st.dns.join(", ")}` : `${s.domain} doesn't point anywhere yet`,
      st.dns_ok ? null : `In your domain's DNS settings, add an A record for ${s.domain} with the value ${ip}. Changes can take a few minutes to an hour.`),
    check(st.router.ok, st.router.ok ? `Your router forwards ports 443 and 80 here (${st.router.method})` : "Your router isn't forwarding the ports yet",
      st.router.ok ? null : `Forward TCP 443 → ${st.lan_ip || "this server"}:${st.ports.https} and TCP 80 → ${st.lan_ip || "this server"}:${st.ports.http} in your router.${st.router.error ? ` (${st.router.error})` : ""}`),
    check(st.certificate_busy ? "busy" : st.certificate_ok, st.certificate_busy ? "Getting a certificate from Let's Encrypt…"
      : st.certificate_ok ? `Certificate valid until ${fmtDateTime(cert.expires_at)}, renewed automatically` : "No certificate yet",
      !st.certificate_busy && !st.certificate_ok ? (st.certificate_error || "Paddock asks Let's Encrypt once the domain points here and the ports are open.") : null),
    !st.certificate_busy && !st.certificate_ok ? h("button", { class: "btn sm", onclick: async () => { await api.post("/api/admin/public/certificate"); toast("Trying again…"); setTimeout(redraw, 1500); } }, icon("refresh", "sm"), "Get the certificate now") : null,
    st.viewers ? h("p", { class: "muted small" }, `${st.viewers} watching right now`) : null);
  box.replaceChildren(
    status || h("ol", { class: "muted small", style: { marginTop: 0, paddingLeft: "20px" } },
      h("li", null, "Pick a name on a domain you own, like videos.example.com, and point it at ", h("b", null, ip), " (a DNS A record)."),
      h("li", null, "Paddock opens ports 443 and 80 on your router, or you forward them yourself."),
      h("li", null, "Paddock gets a free certificate from Let's Encrypt and renews it on its own.")),
    h("div", { class: "row wrap share-form", style: { alignItems: "flex-start", gap: "12px", marginTop: "12px" } },
      field("Domain", domain), field("Email for certificate notices", email)),
    field("People watching at once", viewers, "Your home upload speed is the limit. Each viewer gets up to 1080p."),
    switchEl("Set up my router automatically (UPnP / NAT-PMP)", auto, (v) => { auto = v; }),
    h("label", { class: "row small", style: { margin: "12px 0" } }, agreed,
      h("span", null, "I accept the ", h("a", { href: AGREEMENT, target: "_blank", rel: "noopener noreferrer" }, "Let's Encrypt Subscriber Agreement"))),
    h("div", { class: "row wrap" },
      h("button", { class: "btn accent", onclick: () => save(true) }, s.enabled ? "Save" : "Turn on public links"),
      s.enabled ? h("button", { class: "btn ghost", onclick: async () => {
        if (await confirmDialog("Turn off public links?", "Every public link stops working until you turn them on again. Your links are kept.", { ok: "Turn off", danger: true })) save(false);
      } }, "Turn off") : null));
}

async function drawLinks(box, first = false) {
  const links = await api.get("/api/public-links", { quiet: true }).catch(() => []);
  if (!first && !box.isConnected) return;
  if (!links.length) { box.replaceChildren(h("p", { class: "muted small", style: { margin: 0 } }, "No public links yet.")); return; }
  box.replaceChildren(h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", null, h("tr", null, ...["Video", "Works until", "Views", "Status", ""].map((t) => h("th", null, t)))),
    h("tbody", null, links.map((l) => h("tr", null,
      h("td", null, h("a", { href: `/watch/${l.item_id}` }, l.item_title || "Video"), l.label ? h("div", { class: "muted small" }, l.label) : null),
      h("td", null, l.expires_at ? fmtDateTime(l.expires_at) : "Until turned off"),
      h("td", null, String(l.views), l.last_view_at ? h("div", { class: "muted small" }, `last ${timeAgo(l.last_view_at)}`) : null,
        l.viewers ? h("div", { class: "muted small" }, `${l.viewers} watching now`) : null),
      h("td", null, statePill(l)),
      h("td", null, ["active", "locked"].includes(l.state) ? h("div", { class: "row wrap", style: { gap: "6px" } },
        h("button", { class: "btn sm", onclick: () => { copyText(l.url); toast("Link copied"); } }, icon("copy", "sm"), "Copy link"),
        h("button", { class: "btn sm ghost", onclick: () => changePassword(l, () => drawLinks(box)) }, "New password"),
        h("button", { class: "btn sm ghost", onclick: async () => {
          if (!(await confirmDialog("Turn off this link?", "Anyone watching is stopped and the link never works again.", { ok: "Turn off", danger: true }))) return;
          await api.del(`/api/public-links/${l.id}`); toast("Link turned off"); drawLinks(box);
        } }, "Turn off")) : null)))))));
}

function changePassword(l, done) {
  const pw = h("input", { class: "input mono", value: newPassword() });
  const m = modal("New password", h("div", null,
    h("p", { class: "muted small", style: { marginTop: 0 } }, "Everyone watching is signed out and needs the new password."),
    h("div", { class: "row" }, pw, iconBtn("refresh", "Another one", () => { pw.value = newPassword(); }))), {
    foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), h("button", { class: "btn accent", onclick: async () => {
      await api.put(`/api/public-links/${l.id}/password`, { password: pw.value });
      copyText(`${l.url}\nPassword: ${pw.value}`); toast("Password changed. Link and password copied."); m.close(); done();
    } }, "Change password")],
  });
}

export async function shareOnlineDialog(item) {
  const st = await api.get("/api/admin/public");
  if (!st.settings.enabled) {
    const m = modal("Share online", h("div", null,
      h("p", { style: { marginTop: 0 } }, "Public links let people outside your home watch a video in their browser, protected by a password you choose."),
      h("p", { class: "muted small" }, "They need a domain you own (like videos.example.com). Setting it up takes a few minutes.")), {
      foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Not now"),
        h("button", { class: "btn accent", onclick: () => { m.close(); navigate("/admin/public"); } }, "Set up public links")],
    });
    return;
  }
  const pw = h("input", { class: "input mono", value: newPassword(), maxlength: 256 });
  const days = h("select", { class: "input" }, DURATIONS.map(([v, l]) => h("option", { value: v, selected: v === 7 }, l)));
  const note = h("input", { class: "input", maxlength: 120, placeholder: "Optional, e.g. Here's the race from Saturday!" });
  let download = false;
  const body = h("div", { class: "stack" },
    !st.certificate_ok ? h("div", { class: "callout warn" }, icon("warning"), h("div", { class: "small" }, "The certificate for ", h("b", null, st.settings.domain), " isn't ready yet. The link starts working once it is (see Manage server → Public links).")) : null,
    field("Password", h("div", { class: "row" }, pw, iconBtn("refresh", "Another one", () => { pw.value = newPassword(); })), "At least 8 characters. Send it separately from the link if you can."),
    field("Link works for", days),
    field("Note for viewers", note),
    item.kind === "video" ? switchEl("Allow downloading the original file", false, (v) => { download = v; }) : null);
  const create = h("button", { class: "btn accent" }, icon("globe", "sm"), "Create link");
  const m = modal(`Share "${item.title}" online`, body, { wide: true, foot: [h("button", { class: "btn ghost", onclick: () => m.close() }, "Cancel"), create] });
  create.onclick = async () => {
    if (pw.value.length < 8) return toast("The password needs at least 8 characters", { error: true });
    create.disabled = true;
    try {
      const l = await api.post("/api/public-links", { item_id: item.id, password: pw.value, days: +days.value || null, allow_download: download, label: note.value });
      const both = `${item.title}\n${l.url}\nPassword: ${pw.value}`;
      copyText(both);
      body.replaceChildren(
        h("div", { class: "callout ok" }, icon("check"), h("div", { class: "small" }, "Link and password copied. Paste them to the people you want to share with.")),
        field("Link", h("code", { class: "mono", style: { wordBreak: "break-all" } }, l.url)),
        field("Password", h("code", { class: "mono" }, pw.value)),
        h("div", { class: "row wrap" },
          h("button", { class: "btn", onclick: () => { copyText(both); toast("Copied"); } }, icon("copy", "sm"), "Copy link and password"),
          h("button", { class: "btn ghost", onclick: () => { copyText(l.url); toast("Copied"); } }, "Copy link only")));
      create.replaceWith(h("button", { class: "btn primary", onclick: () => m.close() }, "Done"));
    } finally { create.disabled = false; }
  };
}
