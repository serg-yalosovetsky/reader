// Офлайн-кэш книг: качаем EPUB в Cache API и читаем cache-first, чтобы книга
// открывалась без сети (телефон, плохой коннект). Отдельный индекс id →
// localStorage для мгновенной отрисовки бейджей на карточках (без обхода
// Cache API на каждую карточку). Service worker (sw.js) отдаёт эти же книги
// из того же кэша при офлайн-навигации.
import { logErr } from './log.js'
import { signalAuthRequired } from './api.js'

const BOOKS_CACHE = 'reader-books-v1'      // ДОЛЖЕН совпадать с sw.js
const INDEX_KEY = 'reader:offline-books'   // JSON-массив work.id
// Что именно сохранено: {id: {chapters, savedAt}}. Отдельным ключом, а не
// вместо INDEX_KEY, — чтобы не ломать формат, который читают карточки библиотеки.
// Без этого офлайн-копия неотличима от свежей: книга докачалась на сервере, а в
// кэше лежит старый файл, и SW отдаёт его cache-first — новых глав не видно.
const META_KEY = 'reader:offline-meta'

export const offlineSupported = typeof caches !== 'undefined'

export function bookUrl(id) { return `/api/reader/${id}/file` }

// ---- индекс (быстрый, синхронный) ----
export function offlineIds() {
  try {
    const a = JSON.parse(localStorage.getItem(INDEX_KEY) || '[]')
    return Array.isArray(a) ? a : []
  } catch { return [] }
}
export function isOffline(id) { return offlineIds().includes(id) }
function setIndex(ids) { localStorage.setItem(INDEX_KEY, JSON.stringify([...new Set(ids)])) }
function addIndex(id) { setIndex([...offlineIds(), id]) }
function removeIndex(id) { setIndex(offlineIds().filter((x) => x !== id)) }

// ---- что сохранено (глав в офлайн-копии) ----
function allMeta() {
  try { return JSON.parse(localStorage.getItem(META_KEY) || '{}') || {} } catch { return {} }
}
export function offlineMeta(id) { return allMeta()[id] || null }
function setMeta(id, data) {
  const m = allMeta()
  if (data) m[id] = data; else delete m[id]
  localStorage.setItem(META_KEY, JSON.stringify(m))
}

// Офлайн-копия отстала от того, что уже лежит на сервере.
// null = книга не в офлайне либо сравнить нечем (старая запись без метаданных).
export function offlineStaleBy(work) {
  if (!work || !isOffline(work.id)) return null
  const meta = offlineMeta(work.id)
  const have = Number(work.chapters_have ?? work.chapters_count) || 0
  if (!meta || !meta.chapters || !have) return null
  return have > meta.chapters ? have - meta.chapters : 0
}

// Ответ книги из кэша (или null). openReader читает его вместо сети (cache-first).
export async function cachedBook(id) {
  if (!offlineSupported) return null
  try {
    const c = await caches.open(BOOKS_CACHE)
    return (await c.match(bookUrl(id))) || null
  } catch { return null }
}

// Скачать книгу в офлайн. onProgress(receivedBytes, totalBytes|null) — опционально.
// chapters — сколько глав в этой версии (из work.chapters_have): по нему потом
// видно, что копия отстала.
export async function downloadBook(id, onProgress, chapters = 0) {
  if (!offlineSupported) throw new Error('Офлайн-кэш не поддерживается браузером')
  const url = bookUrl(id)
  let resp
  try {
    resp = await fetch(url, { credentials: 'same-origin' })
  } catch (e) {
    throw new Error('нет связи с сервером')
  }
  // Протухшая SSO-сессия НЕ выглядит как ошибка: nginx уводит на sso.ibotz.fun,
  // fetch послушно идёт по редиректу и резолвится HTML-страницей входа с
  // ok=true. Без этой проверки в книжный кэш ложилась страница логина — книга
  // «сохранялась» и открывалась битой, а кнопку приходилось жать снова и снова.
  const ctype = resp.headers.get('Content-Type') || ''
  if (resp.status === 401 || resp.redirected || ctype.includes('text/html')) {
    let loginUrl = '/'
    try { loginUrl = (await resp.clone().json()).login_url || '/' } catch { /* не JSON */ }
    throw signalAuthRequired(url, 'GET', loginUrl)
  }
  if (!resp.ok) throw new Error('сервер ответил ' + resp.status)

  let stored
  if (resp.body && onProgress) {
    // Стримим для прогресса и параллельно собираем тело для cache.put.
    const total = Number(resp.headers.get('Content-Length')) || null
    const reader = resp.body.getReader()
    const chunks = []
    let received = 0
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(value)
      received += value.length
      try { onProgress(received, total) } catch { /* ignore */ }
    }
    const type = resp.headers.get('Content-Type') || 'application/epub+zip'
    const blob = new Blob(chunks, { type })
    // Пересобираем Response, сохраняя заголовки (media-type нужен при чтении).
    stored = new Response(blob, { status: 200, statusText: 'OK', headers: resp.headers })
  } else {
    stored = resp
  }

  const c = await caches.open(BOOKS_CACHE)
  await c.put(url, stored)     // Cache API кладёт явным put даже при no-store
  addIndex(id)
  setMeta(id, { chapters: Number(chapters) || 0, savedAt: Date.now() })
  return true
}

// Перекачать офлайн-копию поверх старой: сервер докачал новые главы, а в кэше
// лежит прежний файл. Именно этого действия не хватало — кнопка «Сохранить
// офлайн» у уже сохранённой книги просто УДАЛЯЛА копию.
export async function refreshBook(id, onProgress, chapters = 0) {
  await removeBook(id)
  return downloadBook(id, onProgress, chapters)
}

// Убрать книгу из офлайна.
export async function removeBook(id) {
  removeIndex(id)
  setMeta(id, null)
  if (!offlineSupported) return
  try {
    const c = await caches.open(BOOKS_CACHE)
    await c.delete(bookUrl(id))
  } catch (e) { logErr('offline remove', e) }
}

// Сверить индекс с реальным содержимым Cache API (на старте): вычистить
// «призраки» — браузер мог эвакуировать кэш под давлением места.
export async function reconcileOffline() {
  if (!offlineSupported) return
  try {
    const c = await caches.open(BOOKS_CACHE)
    const keys = await c.keys()
    const present = new Set(
      keys
        .map((r) => {
          const m = new URL(r.url).pathname.match(/^\/api\/reader\/(\d+)\/file$/)
          return m ? Number(m[1]) : null
        })
        .filter((x) => x != null),
    )
    setIndex(offlineIds().filter((id) => present.has(id)))
  } catch (e) { logErr('offline reconcile', e) }
}

// Оценка занятого места (МБ) — для инфо в UI.
export async function offlineUsageMB() {
  try {
    if (navigator.storage?.estimate) {
      const { usage } = await navigator.storage.estimate()
      return usage ? Math.round(usage / 1048576) : null
    }
  } catch { /* ignore */ }
  return null
}
