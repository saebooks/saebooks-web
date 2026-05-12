/* SAE Books service worker — v1
 *
 * Scope: origin root (registered from `/sw.js`).
 *
 * Strategy:
 *   - Cache-first for /static/* (built assets, icons, manifest)
 *   - Network-first for navigations (HTML pages) — fall back to a cached
 *     "offline" page if available, else the browser's default offline UI.
 *   - Network-only (NO cache) for /api/*, all non-GET methods, and any
 *     response with a 401 (logged out — must never serve a stale page to
 *     a different user).
 *
 * Why not cache HTML pages? They're tenant-scoped and rendered per
 * session. Caching them would risk cross-user leakage. We accept that
 * the app is "online-only for writes" — same as the desktop product.
 */

const VERSION = 'sae-books-pwa-v1';
const STATIC_CACHE = `${VERSION}-static`;

// Static assets safe to cache at install time.
const PRECACHE = [
  '/static/tailwind.css',
  '/static/manifest.webmanifest',
  '/static/pwa/icons/icon-192.png',
  '/static/pwa/icons/icon-512.png',
  '/static/pwa/icons/icon-maskable-512.png',
  '/static/pwa/icons/apple-touch-icon-180.png',
  '/static/pwa/icons/favicon-16.png',
  '/static/pwa/icons/favicon-32.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) =>
      // addAll fails atomically — if any precache resource 404s, the
      // whole install fails. Use individual add() with catch to avoid
      // breaking install on a missing optional asset.
      Promise.all(
        PRECACHE.map((url) =>
          cache.add(url).catch((err) =>
            console.warn(`[sw] precache miss ${url}:`, err)
          )
        )
      )
    )
  );
  // Activate immediately on first install — no need to wait for old
  // tabs (we have none yet).
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // Drop caches from previous versions.
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE && k.startsWith('sae-books-pwa-'))
          .map((k) => caches.delete(k))
      );
      // Take control of any already-open pages.
      await self.clients.claim();
    })()
  );
});

function isStatic(url) {
  return url.pathname.startsWith('/static/');
}

function isApi(url) {
  return url.pathname.startsWith('/api/');
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Same-origin only. Don't touch CDN scripts (HTMX, Lucide), Stripe, etc.
  if (url.origin !== self.location.origin) return;

  // Never cache non-GET requests. POST/PUT/PATCH/DELETE always go to
  // network — caching mutations would silently double-submit on retry
  // and break CSRF.
  if (req.method !== 'GET') return;

  // API: network-only, no cache. The API serves tenant-scoped JSON.
  if (isApi(url)) return;

  // Static: cache-first with network fallback + background revalidate.
  if (isStatic(url)) {
    event.respondWith(
      caches.open(STATIC_CACHE).then(async (cache) => {
        const cached = await cache.match(req);
        const network = fetch(req)
          .then((res) => {
            if (res && res.ok) cache.put(req, res.clone());
            return res;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // Navigations (HTML): network-only. Tenant-scoped pages are NEVER
  // cached. If offline, the browser's default offline UI fires; that's
  // intentional for v0.9 — a cached HTML page from another session
  // would risk showing stale data or another user's content.
  // We DO inspect the response: a 401 means the session expired or a
  // different user is now logged in — purge any stray dynamic cache
  // entries as a belt-and-braces measure.
  if (req.mode === 'navigate' || (req.headers.get('Accept') || '').includes('text/html')) {
    event.respondWith(
      fetch(req)
        .then(async (res) => {
          if (res.status === 401) {
            // Hard-purge anything we might have stashed dynamically.
            const keys = await caches.keys();
            await Promise.all(
              keys
                .filter((k) => k.startsWith('sae-books-pwa-') && k !== STATIC_CACHE)
                .map((k) => caches.delete(k))
            );
          }
          return res;
        })
        .catch(() => {
          // Offline. Browser shows its own offline page; we don't
          // attempt a fallback because we have no safe page to serve.
          return new Response(
            '<!doctype html><meta charset=utf-8><title>Offline</title>' +
              '<style>body{font-family:system-ui;background:#194291;color:#fff;' +
              'display:flex;flex-direction:column;align-items:center;justify-content:center;' +
              'height:100vh;margin:0;padding:24px;text-align:center}' +
              'h1{font-weight:800;letter-spacing:-0.025em}</style>' +
              '<h1>SAE Books</h1>' +
              '<p>You appear to be offline. SAE Books needs a connection to your ledger ' +
              'to load this page. We will reconnect when you do.</p>' +
              '<p><a href="/" style="color:#fff;text-decoration:underline">Try again</a></p>',
            {
              status: 503,
              statusText: 'Offline',
              headers: { 'Content-Type': 'text/html; charset=utf-8' },
            }
          );
        })
    );
    return;
  }

  // Default: network-only.
});

// Allow the page to ping the SW to skipWaiting on update.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
