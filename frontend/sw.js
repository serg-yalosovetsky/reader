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

const SHELL_CACHE = 'reader-shell-v5'
const API_CACHE = 'reader-api-v1'
const BOOKS_CACHE = 'reader-books-v1'      // ДОЛЖЕН совпадать с offline.js
const KEEP = new Set([SHELL_CACHE, API_CACHE, BOOKS_CACHE])

// Запасной список на случай, если /api/shell-assets недоступен при установке.
// Основной список отдаёт сервер обходом каталога (см. FALLBACK ниже и
// collectAssets): руками этот перечень уже разъезжался — новый модуль
// появлялся, строку сюда дописать забывали, и без сети недостающий импорт
// ронял загрузку оболочки целиком.
const FALLBACK_PRECACHE = [
  '/', '/index.html', '/css/theme.css',
  '/favicon.svg', '/manifest.webmanifest',
  '/js/app.js', '/js/library.js', '/js/reader-core.js', '/js/book-page.js',
  '/js/navigation.js', '/js/bookmarks.js', '/js/highlights.js', '/js/search.js',
  '/js/progress-bar.js',
  '/js/settings.js', '/js/tts.js', '/js/accounts.js',
  '/js/core/api.js', '/js/core/dom.js', '/js/core/state.js', '/js/core/prefs.js',
  '/js/core/log.js', '/js/core/offline.js', '/js/core/position.js',
  '/js/core/locator.js', '/js/core/convert.js', '/js/core/inline-images.js',
]

// Что класть в офлайн-кэш оболочки. Сервер считает список сам (все .js, стили,
// иконки, манифест) — включая vendor/foliate-js, без которого `js/app.js` не
// выполняется вовсе: он импортирует `vendor/foliate-js/view.js` первой строкой,
// и офлайн-старт падал ещё до библиотеки.
async function collectAssets() {
  try {
    const resp = await fetch('/api/shell-assets', { cache: 'no-store' })
    if (resp.ok) {
      const data = await resp.json()
      if (Array.isArray(data?.assets) && data.assets.length) return data.assets
    }
  } catch { /* нет сети или старый бэкенд — берём запасной список */ }
  return FALLBACK_PRECACHE
}

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    try {
      const c = await caches.open(SHELL_CACHE)
      const assets = await collectAssets()
      // Поштучно, а не addAll: тот атомарен — один 404 оставлял кэш ПУСТЫМ,
      // и офлайн-старта не было вообще, хотя установка «прошла».
      await Promise.allSettled(assets.map((u) => c.add(u)))
    } catch { /* установку не валим: оболочка доберёт своё сетевым путём */ }
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
// Потолок ожидания для НАВИГАЦИЙ: их нельзя закрывать кэшем, пока не ясно, что
// сеть действительно мертва (иначе прячем форму входа), но и ждать без предела
// нельзя. См. навигационную ветку networkFirst.
const NAV_NET_TIMEOUT_MS = 8000
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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
  // Сырой ответ сети, каким бы он ни был. Нужен, чтобы отличить «сеть умерла»
  // от «сеть жива, но сервер отправил на вход»: во втором случае ответ ЕСТЬ.
  // Проверять `resp.redirected` тут нельзя — у навигаций режим редиректа
  // `manual`, и fetch резолвится opaqueredirect-ответом, у которого status=0 и
  // redirected=false. Единственный надёжный признак — сам факт, что fetch
  // ответил, а не отказал.
  let rawResp = null
  const net = (async () => {
    const resp = await fetch(req)
    rawResp = resp
    if (!isOwnResponse(resp)) throw new Error('sso-redirect-or-opaque')
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
      // Ждём ответа дольше обычного, но НЕ бесконечно: 2.5 с здесь мало (сеть
      // может отвечать медленно), а мёртвый captive-portal висит минутами, и
      // без второго потолка старт офлайн-читалки на плохом мобильном канале
      // выродился бы в долгое белое окно.
      await Promise.race([net.catch(() => {}), sleep(NAV_NET_TIMEOUT_MS)])
      // Сеть ответила — отдаём её ответ как есть. Для протухшей сессии это
      // редирект (в т.ч. opaqueredirect) на форму входа, и браузер по нему
      // пойдёт: только так пользователь может перелогиниться.
      if (rawResp) return rawResp
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
