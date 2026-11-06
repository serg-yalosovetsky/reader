import assert from 'node:assert/strict'
const storage = new Map()
globalThis.localStorage = { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v) }
let copy, downloads
const cache = { match: async () => copy?.clone(), put: async (_, r) => { copy = r.clone() } }
globalThis.caches = { open: async () => cache }
const { checkedOfflineBook, offlineMeta } = await import('../../frontend/js/core/offline.js')
function reset(rev) {
  copy = new Response('old book', { headers: { 'X-Book-Rev': 'old' } })
  storage.set('reader:offline-meta', JSON.stringify({ 1: { rev } }))
  downloads = 0
}
function network(rev) {
  globalThis.fetch = async (url, opts) => {
    assert.equal(opts.cache, 'no-store')
    if (url.includes('/library/')) return Response.json({ file_rev: rev, chapters_count: 20 })
    downloads++
    return new Response('new book', { headers: { 'X-Book-Rev': rev } })
  }
}
reset('old'); network('new')
let result = await checkedOfflineBook(1)
assert.equal(await result.response.text(), 'new book')
assert.equal(offlineMeta(1).rev, 'new')
assert.equal(downloads, 1)
assert.equal(result.uncertain, false)
reset(undefined); network('new')
await checkedOfflineBook(1)
assert.equal(downloads, 1)
reset('old'); network('old')
result = await checkedOfflineBook(1)
assert.equal(await result.response.text(), 'old book')
assert.equal(downloads, 0)
reset('old'); globalThis.fetch = () => new Promise(() => {})
const start = Date.now()
result = await checkedOfflineBook(1, { timeout: 15 })
assert.ok(Date.now() - start < 500)
assert.equal(result.uncertain, true)
assert.equal(await result.response.text(), 'old book')
reset('old'); globalThis.fetch = async () => { throw new Error('offline') }
result = await checkedOfflineBook(1)
assert.equal(result.uncertain, true)
assert.equal(await result.response.text(), 'old book')
reset('old'); network('new')
const fetchOK = globalThis.fetch
globalThis.fetch = async (url, opts) => url.includes('/library/') ? fetchOK(url, opts) : new Response('', { status: 503 })
result = await checkedOfflineBook(1)
assert.equal(result.uncertain, true)
assert.equal(await copy.text(), 'old book')
console.log('ok: offline revision stale, unknown, equal, timeout, offline, failed refresh')

// The SW must honor the explicit network bypass, including metadata checks.
const { readFile } = await import('node:fs/promises')
const { runInNewContext } = await import('node:vm')
const handlers = {}
let matched = 0, fetched = 0
runInNewContext(await readFile(new URL('../../frontend/sw.js', import.meta.url), 'utf8'), {
  self: { location: { origin: 'https://reader.test' }, addEventListener: (name, fn) => { handlers[name] = fn } },
  URL, Response, Request, Set, console, setTimeout,
  caches: { open: async () => ({ match: async () => { matched++; return new Response('cached') } }) },
  fetch: async () => { fetched++; return new Response('network') },
})
async function swGet(path, cache) {
  let response
  handlers.fetch({ request: new Request('https://reader.test' + path, { cache }), respondWith: r => { response = r } })
  return (await response).text()
}
assert.equal(await swGet('/api/reader/1/file', 'no-store'), 'network')
assert.equal(await swGet('/api/library/1', 'no-store'), 'network')
assert.equal(matched, 0)
assert.equal(fetched, 2)
assert.equal(await swGet('/api/reader/1/file', 'default'), 'cached')
console.log('ok: service worker network bypass and offline fallback')

globalThis.window = new EventTarget()
globalThis.document = new EventTarget()
const { commitPosition, pendingFor, replayPending } = await import('../../frontend/js/core/progress-sync.js')
let conflict
document.addEventListener('reader-revision-conflict', e => { conflict = e.detail })
globalThis.fetch = async () => Response.json({ progress: { locator: 'phone' }, file_rev: 'new' }, { status: 409 })
assert.equal(await commitPosition(1, { ratio: .5, book_rev: 'old' }), false)
assert.equal(pendingFor(1), null)
assert.equal(conflict.progress.locator, 'phone')
assert.equal(conflict.file_rev, 'new')
globalThis.fetch = async () => { throw new Error('rejected position must not replay') }
await replayPending()
console.log('ok: 409 dispatches authoritative position and removes rejected replay')
