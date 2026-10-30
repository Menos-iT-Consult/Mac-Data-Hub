// ── Cache version — bump this string to invalidate all caches on next deploy ──
const VERSION = 'v8';

const STATIC_CACHE  = `mdh-static-${VERSION}`;
const DYNAMIC_CACHE = `mdh-dynamic-${VERSION}`;
const IMAGE_CACHE   = `mdh-images-${VERSION}`;

// Pre-cached app shell — everything needed for offline/PWA launch
const STATIC_ASSETS = [
  // Offline fallback
  '/offline',

  // PWA manifest
  '/static/manifest.json',

  // Favicon
  '/static/favicon.ico',
  '/static/icons/favicon-16.png',
  '/static/icons/favicon-32.png',

  // All PWA icons
  '/static/icons/icon-72.png',
  '/static/icons/icon-96.png',
  '/static/icons/icon-128.png',
  '/static/icons/icon-144.png',
  '/static/icons/icon-152.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-384.png',
  '/static/icons/icon-512.png',

  // Public pages only — auth/dashboard are network-only (always fresh)
  '/',
  '/apply',
];

// ── Helpers ────────────────────────────────────────────────────────────────────

function staticCacheKey(request) {
  const url = new URL(request.url);
  url.search = '';
  return new Request(url.toString(), { headers: request.headers });
}

function isStaticAsset(url) {
  const p = url.pathname;
  return p.startsWith('/static/') || p === '/offline';
}

function isImageAsset(url) {
  return /\.(png|jpg|jpeg|webp|svg|gif|ico)$/i.test(url.pathname);
}

// Routes that must always go to the network — never serve stale data here
function isNetworkOnly(url) {
  const p = url.pathname;
  return (
    p.startsWith('/push/') ||
    p.startsWith('/checkout') ||
    p.startsWith('/verify-payment') ||
    p.startsWith('/track') ||
    p.startsWith('/logout') ||
    p.startsWith('/login') ||
    p.startsWith('/register') ||
    p.startsWith('/auth/') ||
    p.startsWith('/admin/') ||
    p.startsWith('/dashboard/') ||
    p.startsWith('/webhook/')
  );
}

function trimCache(cacheName, maxItems) {
  caches.open(cacheName).then(cache =>
    cache.keys().then(keys => {
      if (keys.length > maxItems) {
        cache.delete(keys[0]).then(() => trimCache(cacheName, maxItems));
      }
    })
  );
}

// ── Install: pre-cache the full app shell ─────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(STATIC_CACHE)
      .then(cache =>
        // addAll fails if any URL errors; use individual puts so one 404 doesn't
        // break the whole install (e.g. dashboard pages require auth)
        Promise.allSettled(
          STATIC_ASSETS.map(url =>
            fetch(url, { credentials: 'include' })
              .then(resp => { if (resp.ok || resp.status === 0) cache.put(url, resp); })
              .catch(() => {})
          )
        )
      )
      .then(() => self.skipWaiting())
  );
});

// ── Activate: purge all stale caches from old versions ────────────────────────
self.addEventListener('activate', e => {
  const keep = new Set([STATIC_CACHE, DYNAMIC_CACHE, IMAGE_CACHE]);
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => !keep.has(k)).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Fetch ──────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  const url = new URL(e.request.url);

  // Never intercept cross-origin requests (Paystack, Google Fonts, CDNs, etc.)
  if (url.origin !== self.location.origin) return;

  // Network-only routes — always live, no caching
  if (isNetworkOnly(url)) {
    e.respondWith(fetch(e.request));
    return;
  }

  // Static assets — cache-first, update in background
  if (isStaticAsset(url)) {
    const key = staticCacheKey(e.request);
    e.respondWith(
      caches.open(STATIC_CACHE).then(cache =>
        cache.match(key).then(cached => {
          const networkFetch = fetch(e.request).then(resp => {
            if (resp.ok) cache.put(key, resp.clone());
            return resp;
          }).catch(() => cached);
          return cached || networkFetch;
        })
      )
    );
    return;
  }

  // Images — cache-first with dedicated image cache (longer-lived)
  if (isImageAsset(url)) {
    e.respondWith(
      caches.open(IMAGE_CACHE).then(cache =>
        cache.match(e.request).then(cached => {
          if (cached) return cached;
          return fetch(e.request).then(resp => {
            if (resp.ok) {
              cache.put(e.request, resp.clone());
              trimCache(IMAGE_CACHE, 60);
            }
            return resp;
          }).catch(() => cached);
        })
      )
    );
    return;
  }

  // HTML navigation — network-first, fall back to cache, then offline page
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(resp => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(DYNAMIC_CACHE).then(c => {
              c.put(e.request, clone);
              trimCache(DYNAMIC_CACHE, 50);
            });
          }
          return resp;
        })
        .catch(() =>
          caches.match(e.request)
            .then(cached => cached || caches.match('/offline'))
        )
    );
    return;
  }

  // Everything else — stale-while-revalidate
  e.respondWith(
    caches.open(DYNAMIC_CACHE).then(cache =>
      cache.match(e.request).then(cached => {
        const fetchPromise = fetch(e.request).then(resp => {
          if (resp.ok) {
            cache.put(e.request, resp.clone());
            trimCache(DYNAMIC_CACHE, 60);
          }
          return resp;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    )
  );
});

// ── Message: handle SKIP_WAITING from update flow ─────────────────────────────
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

// ── Push: show notification ────────────────────────────────────────────────────
self.addEventListener('push', e => {
  let data = {
    title: 'Mac Data Hub',
    body:  'You have a new notification.',
    url:   '/',
    icon:  '/static/icons/icon-192.png',
    type:  'order',
  };
  if (e.data) {
    try { data = { ...data, ...JSON.parse(e.data.text()) }; } catch (_) {}
  }

  const tag = data.type === 'broadcast' ? 'mdh-broadcast' : 'mdh-order';

  e.waitUntil(
    self.registration.showNotification(data.title, {
      body:     data.body,
      icon:     data.icon,
      badge:    '/static/icons/icon-192.png',
      data:     { url: data.url },
      vibrate:  [200, 100, 200],
      tag:      tag,
      renotify: true,
    })
  );
});

// ── Notification click ─────────────────────────────────────────────────────────
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const client of list) {
        if (client.url === target && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(target);
    })
  );
});
