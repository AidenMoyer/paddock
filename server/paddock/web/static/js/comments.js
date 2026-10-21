// Comments under a video, photo or file. Each comment lives in a community (or with everyone on the server)
// and can point at a moment in a video. New comments arrive live (see live.js).

import { api } from "./api.js";
import { ctx } from "./app.js";
import { h, icon, iconBtn, timeAgo, toast, confirmDialog, fmtDuration } from "./ui.js";

const PLACE_KEY = "paddock-comment-place";

export function commentsPanel(itemId, { player = null } = {}) {
  const list = h("div");
  const count = h("span", { class: "muted small" });
  const el = h("section", { class: "comments" }, h("h3", null, icon("chat", "sm"), "Comments", count), list);
  let comments = [], places = [];

  const at = (c) => (c.at != null && player ? h("span", { class: "at", title: "Jump to this moment", onclick: () => {
    player.seek?.(c.at); player.video?.play?.();
  } }, fmtDuration(c.at)) : c.at != null ? h("span", null, fmtDuration(c.at)) : null);

  const row = (c) => {
    const body = h("div", { class: "body" }, c.body);
    const actions = h("div", { class: "actions" },
      c.mine ? h("button", { class: "btn link sm", onclick: () => edit(c, body) }, "Edit") : null,
      c.mine || ctx.user.is_admin ? h("button", { class: "btn link sm", onclick: async () => {
        if (!(await confirmDialog("Delete this comment?", "", { ok: "Delete", danger: true }))) return;
        await api.del(`/api/comments/${c.id}`);
        comments = comments.filter((x) => x.id !== c.id); draw();
      } }, "Delete") : null);
    return h("div", { class: "comment", "data-id": c.id },
      h("span", { class: "avatar" }, (c.display_name || c.username)[0]),
      h("div", null,
        h("div", { class: "meta" }, h("b", null, c.display_name), places.length > 1 || c.community_id ? h("span", { class: "pill" }, c.community) : null,
          h("span", null, timeAgo(c.created_at)), c.edited_at ? h("span", null, "· edited") : null, at(c)),
        body, actions));
  };

  async function edit(c, body) {
    const ta = h("textarea", { class: "input", maxlength: 4000 }, c.body);
    const save = h("button", { class: "btn sm accent", onclick: async () => {
      const r = await api.patch(`/api/comments/${c.id}`, { body: ta.value });
      Object.assign(c, r); draw();
    } }, "Save");
    body.replaceWith(h("div", null, ta, h("div", { class: "row", style: { marginTop: "6px" } }, h("button", { class: "btn sm ghost", onclick: draw }, "Cancel"), save)));
    ta.focus();
  }

  function composer() {
    if (!places.length) return h("p", { class: "muted small" }, "Comments are switched off for your communities.");
    const ta = h("textarea", { class: "input", placeholder: "Add a comment…", maxlength: 4000, rows: 1 });
    const saved = localStorage.getItem(PLACE_KEY);
    const where = h("select", { class: "input", style: { width: "auto" }, title: "Who sees this comment" },
      places.map((p) => h("option", { value: p.id ?? "", selected: String(p.id ?? "") === saved }, p.id == null ? "Everyone" : p.name)));
    const moment = player ? h("label", { class: "row small muted", style: { gap: "6px" } }, h("input", { type: "checkbox" }), "At the current moment") : null;
    const post = h("button", { class: "btn sm accent", disabled: true, onclick: async () => {
      const text = ta.value.trim();
      if (!text) return;
      post.disabled = true;
      try {
        const c = await api.post(`/api/items/${itemId}/comments`, {
          body: text, community_id: where.value ? +where.value : null,
          at: moment?.querySelector("input").checked ? Math.floor(player.currentTime()) : null,
        });
        localStorage.setItem(PLACE_KEY, where.value);
        ta.value = "";
        if (!comments.some((x) => x.id === c.id)) comments.push(c);
        draw();
      } finally { post.disabled = !ta.value.trim(); }
    } }, "Comment");
    ta.addEventListener("input", () => { post.disabled = !ta.value.trim(); ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight}px`; });
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) post.click(); });
    return h("div", { class: "comment-box" }, h("span", { class: "avatar" }, (ctx.user.display_name || ctx.user.username)[0]),
      h("div", null, ta, h("div", { class: "row" }, places.length > 1 ? where : null, moment, h("span", { class: "spacer" }), post)));
  }

  function draw() {
    count.textContent = comments.length ? `· ${comments.length}` : "";
    list.replaceChildren(composer(), ...comments.map(row),
      comments.length ? null : h("p", { class: "muted small" }, "No comments yet."));
  }

  async function load() {
    try {
      const r = await api.get(`/api/items/${itemId}/comments`, { quiet: true });
      comments = r.comments; places = r.places;
      draw();
    } catch { el.hidden = true; }
  }

  const onEvent = (e) => {
    const m = e.detail;
    if (m.type === "comment" && m.comment.item_id === itemId && !comments.some((x) => x.id === m.comment.id)) {
      comments.push(m.comment); draw();
    }
  };
  window.addEventListener("paddock:event", onEvent);
  load();
  return { el, destroy: () => window.removeEventListener("paddock:event", onEvent) };
}

/** A compact "jump to" for a player's time, used by the comment panel. */
export function playerSeekAdapter(player) {
  return { seek: (t) => player.seek(t), currentTime: () => player.currentTime(), video: player.video };
}
