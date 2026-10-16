// Недавние файлы отделены от явно сохранённых офлайн книг.
export const RECENT_CACHE = 'reader-recent-books-v1'
export const MAX_BYTES = 400 * 1048576
export function lruVictims(entries, key, size, limit = MAX_BYTES, count = 5) {
  const old = entries.filter(e => e.key !== key).sort((a, b) => a.used - b.used)
  let bytes = old.reduce((n, e) => n + e.size, size), total = old.length + 1
  const victims = []
  for (const e of old) {
    if (bytes <= limit && total <= count) break
    victims.push(e.key); bytes -= e.size; total--
  }
  return victims
}
export const fileSource = (offline, recent) => offline ? 'offline' : recent ? 'recent' : 'network'
export const changedFile = (cached, response) => response.status === 200
  && (!cached.headers.get('ETag') || cached.headers.get('ETag') !== response.headers.get('ETag'))
function recentUsed(url, response) {
  try { return Number(localStorage.getItem(`reader:recent-used:${url}`)) || Number(response.headers.get('X-Recent-Used')) || 0 } catch { return Number(response.headers.get('X-Recent-Used')) || 0 }
}
let writes = Promise.resolve()
export async function recentBook(url) {
  try {
    const cache = await caches.open(RECENT_CACHE)
    const hit = await cache.match(url)
    if (hit) {
      // Метаданные лежат вместе с ответом: индекс не расходится с Cache Storage.
      try { localStorage.setItem(`reader:recent-used:${new URL(url, location.href).href}`, String(Date.now())) } catch {}
    }
    return hit
  } catch { return null }
}
export function storeRecent(url, response) {
  // Частичный PDF хранится блоками в reader-pdf-chunks-v1, не как целая книга.
  if (response.status === 206 || response.headers.has('Content-Range')) return Promise.resolve()
  const task = async () => {
    try {
      const blob = await response.blob()
      if (blob.size > MAX_BYTES) return
      const cache = await caches.open(RECENT_CACHE)
      const entries = await Promise.all((await cache.keys()).map(async req => {
        const r = await cache.match(req)
        return { key: req.url, size: Number(r.headers.get('X-Recent-Size')) || 0,
          used: recentUsed(req.url, r) }
      }))
      const key = new URL(url, location.href).href
      const victims = lruVictims(entries, key, blob.size)
      const estimate = await navigator.storage?.estimate?.()
      const reclaimed = entries.filter(e => victims.includes(e.key) || e.key === key).reduce((n, e) => n + e.size, 0)
      if (estimate?.quota && estimate.usage + blob.size - reclaimed > estimate.quota * .95) return
      for (const victim of victims) {
        await cache.delete(victim)
        try { localStorage.removeItem(`reader:recent-used:${victim}`) } catch {}
      }
      const headers = new Headers(response.headers)
      headers.set('X-Recent-Size', String(blob.size)); headers.set('X-Recent-Used', String(Date.now()))
      await cache.put(url, new Response(blob, { headers }))
    } catch { /* Квота, приватный режим: чтение продолжается без кэша. */ }
  }
  writes = writes.then(task, task)
  return writes
}
export async function readWithProgress(response, progress) {
  if (!response.ok || !response.body) return response
  const total = Number(response.headers.get('Content-Length')) || 0
  const reader = response.body.getReader(), chunks = []
  let received = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value); received += value.byteLength
    progress(received, total)
  }
  return new Response(new Blob(chunks, { type: response.headers.get('Content-Type') || '' }),
    { status: response.status, headers: response.headers })
}
