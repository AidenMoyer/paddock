// The built-in assistant (local Qwen3.8 / vLLM / opt-in Claude), with visible tool steps.

import { api } from "../api.js";
import { ctx, setTitle } from "../app.js";
import { renderMarkdown } from "../md.js";
import { h, icon, toast, switchEl } from "../ui.js";

const DEFAULT_SUGGESTIONS = [
  "What did I add this week?",
  "Find RED clips from my last shoot day and make a selects collection",
  "How much free space does the server have?",
  "Group my turbo manifold CAD files into a project",
  "Which videos still need captions?",
  "Plan a video about my most recent project",
];

export async function render(root) {
  setTitle("Assistant");
  root.append(chatPanel({}));
}

export function chatPanel({ projectId = null, compact = false, suggestions = DEFAULT_SUGGESTIONS }) {
  let conversationId = null;
  const log = h("div", { class: "log", "aria-live": "polite" });
  const input = h("textarea", { rows: 1, placeholder: "Ask about your media, footage, files or server…", "aria-label": "Message" });
  const send = h("button", { class: "btn accent", type: "submit", "aria-label": "Send", style: { height: "48px", borderRadius: "24px" } }, icon("send"));
  const form = h("form", null, input, send);
  const status = h("div", { class: "muted small", style: { textAlign: "center" } });
  const wrap = h("div", { class: "chat", style: compact ? { height: "70vh" } : null }, log, form, status);

  api.get("/api/ai/info").then((info) => {
    ctx.aiInfo = info;
    if (!info.available) {
      log.append(h("div", { class: "empty" }, icon("sparkle"), h("h2", null, "The assistant isn't set up yet"),
        h("p", null, ctx.user.is_admin ? "Pick a model in Manage server → Assistant. A local Qwen3.8 model keeps everything on your network." : "Ask an admin to choose a model."),
        ctx.user.is_admin ? h("a", { class: "btn accent", href: "/admin/assistant" }, "Set up the assistant") : null));
      input.disabled = send.disabled = true;
      return;
    }
    status.replaceChildren(
      h("span", null, `${info.model} · `, info.offsite ? h("b", { style: { color: "var(--warn)" } }, "cloud (Anthropic)") : "runs on your server", " · "),
      switchEl(info.can_write ? "Can make changes" : "Read-only", info.can_write, async (on) => {
        await api.patch("/api/me", { allow_ai_changes: on });
        toast(on ? "The assistant can now tag, organise and edit projects (never delete)." : "Assistant is read-only");
      }));
    status.style.display = "flex";
    status.style.justifyContent = "center";
    status.style.alignItems = "center";
    status.style.gap = "8px";
    if (!log.children.length) intro();
  });

  function intro() {
    log.append(h("div", { style: { padding: compact ? "8px" : "40px 8px 0" } },
      compact ? null : h("h1", { style: { fontSize: "28px" } }, `Hi ${ctx.user.display_name || ctx.user.username} 👋`),
      compact ? null : h("p", { class: "muted" }, "I can search everything in Paddock, organise footage and files, check on the server, and plan videos."),
      h("div", { class: "suggestions" }, suggestions.map((s) => h("button", { onclick: () => { input.value = s; submit(); } }, s)))));
  }

  function bubble(role, content) {
    const who = h("div", { class: "who" }, role === "user" ? (ctx.user.username[0] || "?").toUpperCase() : icon("sparkle", "sm"));
    const b = h("div", { class: "bubble" }, content);
    const row = h("div", { class: `msg ${role === "user" ? "user" : "bot"}` }, who, b);
    log.append(row);
    log.scrollTop = log.scrollHeight;
    return b;
  }

  async function submit(e) {
    e?.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    if (log.querySelector(".suggestions")) log.replaceChildren();
    input.value = "";
    autosize();
    bubble("user", text);
    const pending = bubble("bot", h("div", { class: "typing" }, h("span"), h("span"), h("span")));
    send.disabled = true;
    try {
      const r = await api.post("/api/ai/chat", { message: text, conversation_id: conversationId, project_id: projectId }, { quiet: true });
      conversationId = r.conversation_id;
      const steps = r.steps.length ? h("div", { class: "steps" }, r.steps.map((s) => h("span", { class: `pill ${s.ok ? "" : "bad"}`, title: JSON.stringify(s.args) }, icon(s.ok ? "check" : "warning", "xs"), s.tool))) : null;
      pending.replaceChildren(steps || "", h("div", { class: "md", html: renderMarkdown(r.reply) }),
        h("div", { class: "faint small", style: { marginTop: "4px" } }, `${r.model} · ${r.seconds}s`));
    } catch (err) {
      pending.replaceChildren(h("div", { class: "callout bad" }, icon("warning"), err.message));
    }
    send.disabled = false;
    log.scrollTop = log.scrollHeight;
    input.focus();
  }

  const autosize = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 200)}px`; };
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
  form.addEventListener("submit", submit);
  return wrap;
}
