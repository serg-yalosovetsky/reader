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

const SHELL_CACHE = 'reader-shell-v2'
const API_CACHE = 'reader-api-v1'
const BOOKS_CACHE = 'reader-books-v1'      // ДОЛЖЕН совпадать с offline.js
const KEEP = new Set([SHELL_CACHE, API_CACHE, BOOKS_CACHE])

// Минимум для первого офлайн-старта. Остальное осядет в кэш по мере обхода.
// Минимум для первого офлайн-старта — теперь весь граф модулей, а не только
// app.js: он их импортирует, и без сети недостающий модуль ронял загрузку
// оболочки целиком (белый экран вместо библиотеки).
const PRECACHE = [
  '/', '/index.html', '/css/theme.css',
  '/favicon.svg', '/manifest.webmanifest',
  '/js/app.js', '/js/library.js', '/js/reader-core.js', '/js/book-page.js',
  '/js/navigation.js', '/js/bookmarks.js', '/js/highlights.js', '/js/search.js',
  '/js/settings.js', '/js/tts.js', '/js/accounts.js',
  '/js/core/api.js', '/js/core/dom.js', '/js/core/state.js', '/js/core/prefs.js',
  '/js/core/log.js', '/js/core/offline.js', '/js/core/position.js',
  '/js/core/locator.js', '/js/core/convert.js', '/js/core/inline-images.js',
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

// Ответ «наш» только если это не SSO-редирект и не opaque. Важно отличать от
// isCacheable: тот решает, КЛАСТЬ ли в кэш, а этот — ОТДАВАТЬ ли приложению.
// При частичной связи (wifi без интернета, captive portal, роуминг) запрос
// доходит до nginx и получает 302 на sso.ibotz.fun; fetch по умолчанию редирект
// следует и резолвится HTML-страницей логина с ok=true. Раньше такой ответ
// уходил в приложение → api.get делал r.json() → SyntaxError → loadLibrary
// падал на первой строке, и библиотека не рисовалась вообще. Теперь это
// трактуется как отказ сети → отдаём кэш.
// С 2026-08 сам /api/* больше не редиректит: nginx отдаёт на нём 401 с телом
// {error:"sso_required"} — обычный «свой» ответ, он доходит до api.js и
// поднимает баннер перелогина. Проверка ниже осталась для оболочки и
// навигаций, которые по-прежнему уходят на sso.ibotz.fun через 302.
const isOwnResponse = (resp) => resp && resp.type === 'basic' && !resp.redirected

// «Сеть есть, но мёртвая» — самый частый случай в дороге. Без таймаута fetch
// висит десятки секунд, и страница выглядит зависшей, а не офлайновой.
const NET_TIMEOUT_MS = 2500

// Ответ-заглушка вместо ОТКАЗА промиса. e.respondWith(отклонённый промис) даёт
// странице голый `TypeError: Failed to fetch` — без статуса, без тела, не
// отличимый от «сервер недоступен»; приложение не может ни показать причину,
// ни отличить обрыв сети от протухшей сессии. Синтетический 503 с JSON-телом
// проходит по обычному пути обработки ошибок (api.js → ApiError).
const offlineResponse = () =>
  new Response(JSON.stringify({ error: 'offline', detail: 'нет связи с сервером' }), {
    status: 503,
    statusText: 'Offline',
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  })

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  // Ответ страницы входа: сеть ЖИВА, протухла сессия. Отличать это от «нет
  // связи» обязательно — см. навигационную ветку ниже.
  let ssoResponse = null
  const net = (async () => {
    const resp = await fetch(req)
    if (!isOwnResponse(resp)) {
      if (resp && resp.redirected) ssoResponse = resp
      throw new Error('sso-redirect-or-opaque')
    }
    if (isCacheable(resp)) cache.put(req, resp.clone()).catch(() => {})
    return resp
  })()
  net.catch(() => {})   // фон может отвалиться позже — без unhandledrejection

  let timer
  const timeout = new Promise((_, rej) => {
    timer = setTimeout(() => rej(new Error('net-timeout')), NET_TIMEOUT_MS)
  })
  try {
    return await Promise.race([net, timeout])
  } catch {
    // ОТКРЫТИЕ САЙТА при протухшей сессии — отдельный случай, и его нельзя
    // закрывать кэшем. Раньше здесь отдавалась сохранённая оболочка: сеть-то
    // ответила, но редиректом на sso.ibotz.fun, что засчитывалось за отказ.
    // Пользователь получал офлайн-читалку, в которой библиотека рисуется из
    // кэша, любое действие молча падает, а страницу входа он НЕ ВИДИТ НИКОГДА
    // — то есть перелогиниться из приложения физически нельзя, и оно навсегда
    // застревает на старом закэшированном JS. Поэтому для навигаций дожидаемся
    // настоящего ответа: пришёл редирект на вход — отдаём его, пусть браузер
    // покажет форму. Реальный офлайн распознаётся тем, что fetch ОТКАЗАЛ
    // (браузер знает про отсутствие сети и отвечает мгновенно) — тогда, как и
    // раньше, отдаём оболочку и читалка работает без сети.
    if (req.mode === 'navigate') {
      const resp = await net.catch(() => null)
      if (ssoResponse) return ssoResponse
      if (resp) return resp
      const shell = (await caches.match('/index.html')) || (await caches.match('/'))
      if (shell) return shell
      return offlineResponse()
    }
    const hit = await cache.match(req)
    if (hit) return hit                        // кэш есть — отдаём мгновенно
    // Кэша нет — ждём медленную сеть. Но если и она откажет, отдаём 503, а не
    // отклонённый промис: иначе страница получала «Failed to fetch».
    return net.catch(() => offlineResponse())
  } finally {
    clearTimeout(timer)
  }
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName)
  const hit = await cache.match(req)
  if (hit) return hit
  const resp = await fetch(req).catch(() => null)
  if (!resp) return offlineResponse()
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
      return fetch(req).catch(() => offlineResponse())
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
    // Статус фоновой проверки не кэшируем: кэшированный «done» прошлой
    // проверки выглядел бы как итог текущей.
    if (url.pathname === '/api/monitored/check/status') {
      e.respondWith(fetch(req).catch(() => offlineResponse()))
      return
    }
    e.respondWith(networkFirst(req, API_CACHE))
    return
  }
  // Оболочка (навигации + статика html/js/css/svg): network-first.
  e.respondWith(networkFirst(req, SHELL_CACHE))
})
