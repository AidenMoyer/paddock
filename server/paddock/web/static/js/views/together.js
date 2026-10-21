// Together: watch parties happening now, your communities' voice lounges, and calling people.

import { api, thumbUrl } from "../api.js";
import { ctx, navigate, setTitle } from "../app.js";
import { voiceHub, callPeople } from "../live.js";
import { voiceSupported } from "../voice.js";
import { h, icon, toast, emptyState, timeAgo } from "../ui.js";

export async function render(root) {
  setTitle("Together");
  const parties = h("div", { class: "together-grid" });
  const lounges = h("div", { class: "together-grid" });
  const calls = h("div");
  const people = h("div", { class: "people-list" });
  root.append(
    h("div", { class: "page-head" }, h("h1", null, "Together"),
      h("p", { class: "muted", style: { margin: 0 } }, "Watch in sync and talk, wherever everyone is. Voice keeps going while you browse.")),
    h("section", null, h("h2", null, "Happening now"), parties),
    h("section", null, h("h2", null, "Lounges"), h("p", { class: "muted small" }, "Each community's always-open voice room. Drop in and out."), lounges),
    calls,
    h("section", null, h("h2", null, "Call someone"), people));

  const picked = new Set();
  let everyone = [];

  async function draw() {
    const [ps, places] = await Promise.all([
      api.get("/api/parties", { quiet: true }).catch(() => []),
      api.get("/api/voice/places", { quiet: true }).catch(() => ({ lounges: [], calls: [], online: [] })),
    ]);
    if (!root.isConnected && root.dataset.drawn) return;
    root.dataset.drawn = "1";
    parties.replaceChildren(...(ps.length ? ps.map((p) => h("a", { class: "together-card", href: `/watch/${p.item_id}?party=${p.id}` },
      h("div", { class: "thumb" }, h("img", { src: thumbUrl(p.item_id), alt: "", loading: "lazy" })),
      h("div", null, h("b", null, p.title), h("div", { class: "muted small" },
        `${p.host}'s party${p.community ? ` · ${p.community}` : ""} · ${p.members.length} watching${p.queue?.length ? ` · ${p.queue.length} queued` : ""}`)),
      h("span", { class: "btn sm accent" }, "Join")))
      : [h("p", { class: "muted small" }, "Nobody's watching together right now. Start a party from any video with Watch together.")]));

    lounges.replaceChildren(...(places.lounges.length ? places.lounges.map((l) => {
      const here = voiceHub.room === l.room;
      return h("div", { class: `together-card${here ? " here" : ""}` },
        h("div", { class: "lounge-icon" }, icon("users")),
        h("div", { style: { minWidth: 0 } }, h("b", null, l.name),
          h("div", { class: "voice-people" }, ...(l.members.length ? l.members.map((m) => h("span", { class: `avatar sm${m.speaking ? " talking" : ""}`, title: m.username }, m.username[0]))
            : [h("span", { class: "muted small" }, "Empty")]))),
        here ? h("button", { class: "btn sm danger", onclick: () => { voiceHub.leave(); draw(); } }, "Leave")
          : h("button", { class: "btn sm accent", disabled: !voiceSupported(), onclick: async () => { await voiceHub.join(l.room, `${l.name} lounge`); draw(); } }, "Join"));
    }) : [h("p", { class: "muted small" }, ctx.user.is_admin ? "Lounges appear for communities with voice on (Manage server → Communities)." : "You're not in a community with voice switched on.")]));

    calls.replaceChildren(...(places.calls.length ? [h("section", null, h("h2", null, "Your calls"), ...places.calls.map((c) => h("div", { class: "together-card" },
      h("div", { class: "lounge-icon" }, icon("phone")),
      h("div", { style: { minWidth: 0 } }, h("b", null, c.people.map((p) => p.name).join(", ")),
        h("div", { class: "muted small" }, `started by ${c.host} ${timeAgo(c.created)} · ${c.in_call.length} talking`)),
      voiceHub.room === c.room ? h("span", { class: "pill ok" }, "You're in it")
        : h("button", { class: "btn sm accent", onclick: async () => { await voiceHub.join(c.room, `Call with ${c.host}`); draw(); } }, "Join"),
      c.host_id === ctx.user.id ? h("button", { class: "btn sm ghost", onclick: async () => { await api.del(`/api/calls/${c.id}`); draw(); } }, "End") : null)))] : []));

    everyone = await api.get("/api/people", { quiet: true }).catch(() => []);
    const online = new Set(places.online);
    const callBtn = h("button", { class: "btn accent", disabled: !picked.size || !voiceSupported(), onclick: () => start([...picked]) }, icon("phone", "sm"),
      picked.size > 1 ? `Call ${picked.size} people` : "Call");
    people.replaceChildren(...(everyone.length ? [
      ...everyone.map((p) => h("label", { class: "person" },
        h("input", { type: "checkbox", checked: picked.has(p.id), onchange: (e) => { if (e.target.checked) picked.add(p.id); else picked.delete(p.id); draw(); } }),
        h("span", { class: "avatar sm" }, (p.display_name || p.username)[0]),
        h("span", { style: { flex: 1 } }, p.display_name || p.username),
        h("span", { class: `presence${online.has(p.id) ? " on" : ""}` }, online.has(p.id) ? "Online" : "Away"),
        h("button", { class: "btn sm", disabled: !voiceSupported(), onclick: (e) => { e.preventDefault(); start([p.id]); } }, icon("phone", "sm"), "Call"))),
      h("div", { class: "row", style: { marginTop: "12px" } }, callBtn)]
      : [emptyState("users", "No one to call yet", "People in your communities show up here.")]));
  }

  async function start(ids) {
    try {
      await callPeople(ids);
      picked.clear();
      toast("Calling…");
      draw();
    } catch (e) { /* the API already showed why */ }
  }

  const onEvent = (e) => { if (["party", "declined", "call_ended", "ring"].includes(e.detail.type)) draw(); };
  window.addEventListener("paddock:event", onEvent);
  voiceHub.addEventListener("change", draw);
  const timer = setInterval(draw, 8000);
  await draw();
  return { destroy: () => { clearInterval(timer); window.removeEventListener("paddock:event", onEvent); voiceHub.removeEventListener("change", draw); } };
}
