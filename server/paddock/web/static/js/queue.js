// Your play queue (like YouTube's): "Play next" / "Add to queue". Kept per browser.

const KEY = "paddock-queue";
let q = load();

function load() {
  try { return JSON.parse(localStorage.getItem(KEY)) || []; } catch { return []; }
}
function save() {
  try { localStorage.setItem(KEY, JSON.stringify(q)); } catch { /* private mode: queue lives for this tab only */ }
  window.dispatchEvent(new Event("paddock:queue"));
}
const slim = (it) => ({
  id: it.id, title: it.title, kind: it.kind, ext: it.ext, duration: it.duration, has_thumb: it.has_thumb,
  library: it.library, library_kind: it.library_kind, taken_at: it.taken_at, date: it.date, show: it.show,
  season: it.season, episode: it.episode, artist: it.artist, album: it.album, year: it.year,
});

export const queue = {
  items: () => q.slice(),
  add(it) { if (!q.some((x) => x.id === it.id)) { q.push(slim(it)); save(); } },
  playNext(it) { q = q.filter((x) => x.id !== it.id); q.unshift(slim(it)); save(); },
  remove(id) { q = q.filter((x) => x.id !== id); save(); },
  clear() { q = []; save(); },
  /** Called when an item starts playing: it leaves the queue. */
  started(id) { if (q.some((x) => x.id === id)) { q = q.filter((x) => x.id !== id); save(); } },
  next() { return q[0] || null; },
};
