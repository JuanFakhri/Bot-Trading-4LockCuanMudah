/* NestSMC by Draftnest — service worker (installable PWA).
   Shell is cache-first; live data (data/*.json, /api/*) is always network-first
   so signals & backtest stay fresh. */
const CACHE = "nestsmc-v9";
const SHELL = [
  "./", "index.html", "style.css", "app.js",
  "manifest.webmanifest", "icon-192.png", "icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  // Always go to network for dynamic data so the dashboard is never stale.
  if (url.pathname.includes("/data/") || url.pathname.includes("/api/")) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }
  // Shell assets: serve from cache, refresh in the background.
  e.respondWith(
    caches.match(e.request).then(cached => {
      const net = fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      }).catch(() => cached);
      return cached || net;
    })
  );
});
