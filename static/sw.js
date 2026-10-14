// Service worker of the home-screen app. It only makes the app installable and shows a friendly
// page when there is no connection; timetable and live data always come from the network.
const CACHE = "mhd-offline-v1";
const OFFLINE = "/offline";

self.addEventListener("install", event => {
  // put() replaces an existing copy (add() fails in Chrome when the entry already exists).
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => fetch(OFFLINE, { cache: "reload" }).then(response => cache.put(OFFLINE, response)))
      .catch(() => {})  // storage unavailable: still install, just without the offline page
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return;  // API calls, tiles etc. go straight to the network
  event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
});
