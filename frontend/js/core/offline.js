// Офлайн-кэш книг: качаем EPUB в Cache API и читаем cache-first, чтобы книга
// открывалась без сети (телефон, плохой коннект). Отдельный индекс id →
// localStorage для мгновенной отрисовки бейджей на карточках (без обхода
// Cache API на каждую карточку). Service worker (sw.js) отдаёт эти же книги
// из того же кэша при офлайн-навигации.
import { logErr } from './log.js'

const BOOKS_CACHE = 'reader-books-v1'      // ДОЛЖЕН совпадать с sw.js
const INDEX_KEY = 'reader:offline-books'   // JSON-массив work.id

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

// Ответ книги из кэша (или null). openReader читает его вместо сети (cache-first).
export async function cachedBook(id) {
  if (!offlineSupported) return null
  try {
    const c = await caches.open(BOOKS_CACHE)
    return (await c.match(bookUrl(id))) || null
  } catch { return null }
}

// Скачать книгу в офлайн. onProgress(receivedBytes, totalBytes|null) — опционально.
export async function downloadBook(id, onProgress) {
  if (!offlineSupported) throw new Error('Офлайн-кэш не поддерживается браузером')
  const url = bookUrl(id)
  const resp = await fetch(url, { credentials: 'same-origin' })
  if (!resp.ok) throw new Error('HTTP ' + resp.status)

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
  return true
}

// Убрать книгу из офлайна.
export async function removeBook(id) {
  removeIndex(id)
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
