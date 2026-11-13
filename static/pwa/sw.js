/* __BRAND_NAME__ service worker — v1
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

// v2 (b15, P2-25): brand icons, phone helpers and the offline snapshot page.
// The cache name keeps the 'sae-books-pwa-' prefix so activate() still
// clears every older cache.
const VERSION = 'sae-books-pwa-v2';
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
  '/static/brand/tasur-icon-192.png',
  '/static/brand/tasur-icon-512.png',
  '/static/brand/tasur-icon-maskable-512.png',
  '/static/brand/tasur-favicon.png',
  '/static/css/phone.css',
  '/static/js/phone.js',
  '/static/js/inbox_upload.js',
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
          // b15 (P2-25): instead of a blank page, show what was waiting
          // the last time this phone had signal. The dashboard's
          // needs-attention card stores a count-only snapshot (labels and
          // counts, no names or amounts) in localStorage; this page reads
          // it in the browser. Photos taken in the inbox while offline are
          // kept on the phone and sent when the signal returns.
          return new Response(
            '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">' +
              '<title>Offline</title>' +
              '<style>body{font-family:system-ui;background:#0f1a33;color:#fff;' +
              'display:flex;flex-direction:column;align-items:center;justify-content:center;' +
              'min-height:100vh;margin:0;padding:24px;text-align:center}' +
              'h1{font-weight:800;letter-spacing:-0.025em}ul{list-style:none;padding:0;margin:16px 0;text-align:left;min-width:260px}' +
              'li{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.2)}' +
              'small{opacity:.7}a{color:#fff}</style>' +
              '<h1>__BRAND_NAME__</h1>' +
              '<p>No connection just now. __BRAND_NAME__ needs a connection to your ledger to load this page; it will work again when you are back online.</p>' +
              '<div id=snap></div>' +
              '<p><a href="/" style="text-decoration:underline">Try again</a></p>' +
              '<script>(function(){try{var s=JSON.parse(localStorage.getItem("sb_attention_snapshot")||"null");' +
              'if(!s)return;var el=document.getElementById("snap");var h="<p><small>Last checked "+' +
              'new Date(s.at).toLocaleString()+"</small></p>";' +
              'if(!s.items.length){h+="<p>Nothing was waiting.</p>";}else{h+="<p>Waiting then:</p><ul>";' +
              's.items.forEach(function(i){var li=document.createElement("li");var a=document.createElement("span");' +
              'a.textContent=i.label;var b=document.createElement("strong");b.textContent=String(i.count);' +
              'li.appendChild(a);li.appendChild(b);h+=li.outerHTML;});h+="</ul>";}el.innerHTML=h;}catch(e){}})();</script>',
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
