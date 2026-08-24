/* Static-asset cache only (task W1).
   No offline writes in v1 (SPEC 8): the Pi is on the same Wi-Fi as the phones,
   so a sync/conflict layer would be cost without benefit. Anything that is not
   a same-origin GET of a static asset goes straight to the network, so a log
   entry can never be silently served from cache. */
var CACHE = "cradle-static-v2";
var ASSETS = [
  "/static/app.css",
  "/static/manifest.json",
  "/static/icon.svg",
  "/static/vendor/plotly.min.js",
  "/static/chart.js",
  "/static/series.js",
];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(ASSETS); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }));
  self.clients.claim();
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf("/static/") !== 0) return;
  e.respondWith(caches.match(e.request).then(function (hit) {
    return hit || fetch(e.request);
  }));
});
