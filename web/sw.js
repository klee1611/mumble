// App-shell cache so Mumble opens instantly (and offline) from the home screen.
const CACHE = "mumble-v8";
const SHELL = ["/", "/config.js", "/app.js", "/style.css", "/icon.svg", "/manifest.webmanifest",
               "/vendor/auth0-spa-js-2.28.2.esm.js"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Only our own static files: never the API, never Auth0, never a sign-in
  // callback (its ?code= is single-use and shouldn't sit in a cache).
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/") || url.search) return;
  // Network first, cache as fallback: fresh when online, still opens offline.
  e.respondWith(
    // "no-cache" revalidates with the server, so a deploy (or a security fix)
    // reaches the phone on the next open instead of hiding behind the HTTP cache.
    fetch(e.request, { cache: "no-cache" })
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
