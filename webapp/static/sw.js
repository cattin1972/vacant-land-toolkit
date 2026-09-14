// Minimal app-shell service worker -- installability + resilience if a
// request briefly fails, NOT an offline data cache and NOT the thing that
// decides what's "current." Freshness comes from the strategy below
// (network-first for the shell), not from remembering to bump CACHE_NAME
// on every deploy -- bumping it still forces a clean slate (old cache
// entries deleted in `activate`), but a stale deploy should never be
// reachable through this file even if that bump is forgotten.
//
// CHANGED 2026-09-14 (real deployed-vs-local mismatch investigation): the
// first version of this file used cache-first ("return the cached shell
// immediately, refresh the cache in the background for NEXT time"). That
// specific bug did NOT cause the mismatch found this session -- the real
// cause was that a large batch of frontend work had simply never been
// committed/pushed to git at all, so Render had nothing new to deploy in
// the first place (see the project notes for the full timeline). But
// cache-first is still the wrong long-term choice for a shell this early
// in active development: once this file IS actually live, a visitor who
// already has it installed could keep seeing an old cached shell for one
// extra visit after every future deploy, with no visible sign anything
// was stale. Switched to network-first for navigations specifically --
// whenever the network is reachable at all, the freshest HTML always
// wins, and the cache exists purely as an offline/flaky-connection
// fallback, never as a way to defer picking up a new deploy.
const CACHE_NAME = 'vlt-shell-v2';
const SHELL_URLS = ['/'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only ever intercept same-origin GET requests. Every /api/* call in
  // this app carries the CALLER's own Realie/Tracerfy key and returns
  // live, per-search, per-parcel data -- caching or replaying any of it
  // from a service worker would be a real correctness risk (stale
  // government data served as current) even though it's not a security
  // risk (the Cache Storage API is already per-origin, per-browser, same
  // isolation boundary as localStorage). Simplest and safest rule: never
  // touch /api/ at all, and never touch anything but GET.
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) {
    return;
  }

  // Network-first for the page itself (a navigation request, or "/"
  // directly) -- always try to get the CURRENT deploy first; only fall
  // back to whatever shell is cached if the network genuinely fails
  // (offline, DNS hiccup, etc). This is the one request type where
  // "instant from cache" is not worth the risk of showing a stale app.
  const isNavigation = req.mode === 'navigate' || url.pathname === '/';
  if (isNavigation) {
    event.respondWith(
      fetch(req)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return resp;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // Everything else same-origin (fonts/icons served from /static, etc.):
  // cache-first is fine here -- these change far less often, and it's
  // fine for a static icon to lag one deploy behind while the page
  // itself is always guaranteed current above.
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          }
          return resp;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
