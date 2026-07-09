/* Service worker читалки: офлайн-оболочка + отдача сохранённых книг.

   Стратегии:
   • навигации и статика приложения (html/js/css/svg) — network-first:
     онлайн всегда свежий JS (нет застрявшего кэша, как было со stale-модулями),
     офлайн — из кэша; навигация без сети → отдаём оболочку index.html.
   • обложки /api/reader/<id>/cover — cache-first (сервер шлёт immutable).
   • файлы книг /api/reader/<id>/file — из отдельного books-кэша (его наполняет
     страница по кнопке «Сохранить офлайн»); онлайн-фолбэк на сеть.
   • прочие GET /api/* — network-first с кэш-фолбэком (библиотека/прогресс
     открываются офлайн с последними данными).

   НЕ кэшируем: не-GET, кросс-ориджин (Google Fonts и т.п.), редиректы —
   в частности SSO-логин (302 наружу), иначе бы залипала страница входа. */

const SHELL_CACHE = 'reader-shell-v1'
const API_CACHE = 'reader-api-v1'
const BOOKS_CACHE = 'reader-books-v1'      // ДОЛЖЕН совпадать с offline.js
const KEEP = new Set([SHELL_CACHE, API_CACHE, BOOKS_CACHE])

// Минимум для первого офлайн-старта. Остальное осядет в кэш по мере обхода.
const PRECACHE = [
  '/', '/index.html', '/css/theme.css', '/js/app.js',
  '/favicon.svg', '/manifest.webmanifest',
]

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    try {
      const c = await caches.open(SHELL_CACHE)
      await c.addAll(PRECACHE)
    } catch { /* один недоступный ресурс не должен валить установку */ }
    self.skipWaiting()
  })())
})

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (!KEEP.has(k)) await caches.delete(k)
    await self.clients.claim()
  })())
})

self.addEventListener('message', (e) => {
  if (e.data === 'skipWaiting') self.skipWaiting()
})

// Кэшируем только «чистый» свой ответ 200 — не редирект (SSO), не opaque.
const isCacheable = (resp) =>
  resp && resp.ok && resp.status === 200 && resp.type === 'basic' && !resp.redirected

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  try {
    const resp = await fetch(req)
    if (isCacheable(resp)) cache.put(req, resp.clone()).catch(() => {})
    return resp
  } catch (err) {
    const hit = await cache.match(req)
    if (hit) return hit
    if (req.mode === 'navigate') {
      const shell = (await caches.match('/index.html')) || (await caches.match('/'))
      if (shell) return shell
    }
    throw err
  }
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  const hit = await cache.match(req)
  if (hit) return hit
  const resp = await fetch(req)
  if (isCacheable(resp)) cache.put(req, resp.clone()).catch(() => {})
  return resp
}

self.addEventListener('fetch', (e) => {
  const req = e.request
  if (req.method !== 'GET') return
  const url = new URL(req.url)
  if (url.origin !== self.location.origin) return   // Google Fonts и прочее — мимо SW

  // Файл книги: сначала books-кэш (сохранённое офлайн), иначе сеть.
  if (/^\/api\/reader\/\d+\/file$/.test(url.pathname)) {
    e.respondWith((async () => {
      const c = await caches.open(BOOKS_CACHE)
      const hit = await c.match(req.url)
      if (hit) return hit
      return fetch(req)
    })())
    return
  }
  // Обложки: cache-first (immutable).
  if (/^\/api\/reader\/\d+\/cover/.test(url.pathname)) {
    e.respondWith(cacheFirst(req, API_CACHE))
    return
  }
  // Прочие API: network-first с кэш-фолбэком.
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(networkFirst(req, API_CACHE))
    return
  }
  // Оболочка (навигации + статика html/js/css/svg): network-first.
  e.respondWith(networkFirst(req, SHELL_CACHE))
})
