// Paddock service worker: keeps a copy of the app shell so the UI still opens when the server is
// unreachable. Media and API responses are never cached here (they carry auth and can be huge).
//
// Static files are network-first: the app is a graph of ES modules, and serving cached copies first
// (stale-while-revalidate) ran a mix of old and new modules after every update. The server answers
// unchanged files with a cheap 304, so going to the network first costs almost nothing on the LAN.
const SHELL = "paddock-shell-v2";
const ASSETS = ["/", "/static/tokens.css", "/static/app.css", "/static/js/app.js", "/static/icon.svg", "/static/vendor/hls.min.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/s/") || url.pathname === "/mcp") return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.open(SHELL).then((c) => fetch(e.request)
        .then((r) => { if (r.ok) c.put(e.request, r.clone()); return r; })
        .catch(async () => (await c.match(e.request)) || Response.error()))
    );
    return;
  }
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/")));
  }
});
