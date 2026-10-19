import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
const source = readFileSync(new URL('../../frontend/vendor/foliate-js/pdf-range.js', import.meta.url), 'utf8')
const { chunkIndices, joinChunks, prefetchPages, chunkVictims, checkPDFResponse } = await import('data:text/javascript,' + encodeURIComponent(source))
assert.deepEqual(chunkIndices(3, 9, 4), [0, 1, 2])
assert.deepEqual(chunkIndices(4, 8, 4), [1])
assert.deepEqual([...joinChunks([Uint8Array.of(0,1,2,3), Uint8Array.of(4,5,6,7), Uint8Array.of(8,9)], 3, 9, 4)], [3,4,5,6,7,8])
assert.deepEqual(prefetchPages(0, 4), [1,2,3])
assert.deepEqual(prefetchPages(3, 4), [2,1,0])
assert.deepEqual(chunkVictims([{key:'a',size:4,used:2},{key:'b',size:4,used:1}],4,8), ['b'])
assert.throws(() => checkPDFResponse(new Response('<html>', {headers:{'Content-Type':'text/html'}})), /войти заново/)
console.log('PDF range OK')

// Настоящая логика транспорта с подменённой сетью и Cache Storage.
const { openPDFSource, CHUNK_SIZE } = await import('data:text/javascript,' + encodeURIComponent(source))
globalThis.location = { href: 'https://reader.test/' }
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { storage: { estimate: async () => ({quota: 2e9, usage: 0}) } } })
const entries = new Map()
globalThis.caches = { open: async () => ({
  keys: async () => [...entries.keys()].map(url => ({ url })),
  match: async key => entries.get(key.url || key)?.clone(),
  put: async (key, value) => entries.set(key, value.clone()),
  delete: async key => entries.delete(key.url || key),
}) }
const data = Uint8Array.from({length: CHUNK_SIZE * 3 + 13}, (_, n) => n % 251)
let etag = '"v1"', reads = 0
const headers = () => ({ 'ETag': etag, 'Content-Length': data.length, 'Content-Type': 'application/pdf', 'X-Book-SHA1': 'rev' })
globalThis.fetch = async (url, options) => {
  if (options.method === 'HEAD') return new Response(null, {headers: headers()})
  reads++
  assert.equal(options.credentials, 'same-origin')
  assert.equal(options.headers['If-Range'], etag)
  const [begin, last] = options.headers.Range.slice(6).split('-').map(Number)
  assert.equal(begin % CHUNK_SIZE, 0)
  return new Response(data.slice(begin, last+1), {status:206, headers: {...headers(), 'Content-Range': `bytes ${begin}-${last}/${data.length}`}})
}
class Transport {
  constructor(length) { this.length = length }
  onDataRange(begin, bytes) { this.received?.(begin, bytes) }
}
const read = (source, begin, end) => new Promise((resolve, reject) => {
  source.onError(reject)
  source.transport.received = (offset, bytes) => {
    assert.equal(offset, begin)
    assert.deepEqual(bytes, data.slice(begin, end))
    resolve()
  }
  source.transport.requestDataRange(begin, end)
})
const first = await openPDFSource({url:'/book'}, {PDFDataRangeTransport:Transport})
await read(first, CHUNK_SIZE-10, CHUNK_SIZE+10)
assert.equal(reads, 2)
const second = await openPDFSource('/book', {PDFDataRangeTransport:Transport})
await read(second, CHUNK_SIZE-5, CHUNK_SIZE+5)
assert.equal(reads, 2, 'повторное открытие использует постоянные блоки')
etag = '"v2"'
const third = await openPDFSource('/book', {PDFDataRangeTransport:Transport})
assert.equal(entries.size, 0, 'смена ETag удаляет предыдущую редакцию')
await read(third, 0, 10)
assert.equal(reads, 3)
globalThis.fetch = async () => new Response('<html>login</html>', {headers:{'Content-Type':'text/html'}})
await assert.rejects(openPDFSource('/book', {PDFDataRangeTransport:Transport}), /войти заново/)
console.log('PDF transport cache OK')

// HEAD бывает недоступен за прокси. Первый блок сохраняется, даже если pdf.js
// сначала попросил хвост нелинеаризованного документа.
entries.clear()
let fallbackReads = 0
const fallbackFetch = async (url, options) => {
  if (options.method === 'HEAD') return new Response(null, {status:405})
  fallbackReads++
  const [begin, last] = options.headers.Range.slice(6).split('-').map(Number)
  const end = Math.min(last + 1, data.length)
  return new Response(data.slice(begin, end), {status:206, headers:{...headers(), 'Content-Range':`bytes ${begin}-${end-1}/${data.length}`}})
}
globalThis.fetch = fallbackFetch
const fallback = await openPDFSource('/fallback', {PDFDataRangeTransport:Transport})
await read(fallback, CHUNK_SIZE*3, data.length)
await read(fallback, 0, 10)
assert.equal(fallbackReads, 2, 'начальный блок не скачивается повторно после хвоста')
entries.clear()
const changed = await openPDFSource('/changed', {PDFDataRangeTransport:Transport})
globalThis.fetch = async () => new Response(data, {headers:headers()})
await assert.rejects(read(changed, CHUNK_SIZE, CHUNK_SIZE+10), /изменился/)
assert.equal(entries.size, 0)
console.log('PDF fallback / If-Range OK')

// #2038: блок из кэша не той длины перекачивается, а не отдаётся pdf.js.
entries.clear()
etag = '"v1"'
let healReads = 0
globalThis.fetch = async (url, options) => {
  if (options.method === 'HEAD') return new Response(null, {headers: headers()})
  healReads++
  const [begin, last] = options.headers.Range.slice(6).split('-').map(Number)
  return new Response(data.slice(begin, last+1), {status:206, headers: {...headers(), 'Content-Range': `bytes ${begin}-${last}/${data.length}`}})
}
const warn = console.warn; console.warn = () => {}
const heal = await openPDFSource('/heal', {PDFDataRangeTransport:Transport})
const key0 = [...entries.keys()].find(k => k.includes('chunk=0')) ?? new URL('/__reader_pdf_chunk__?' + new URLSearchParams({book:'/heal', etag:'"v1"', chunk:0}), 'https://reader.test/').href
entries.set(key0, new Response(data.slice(0, 100)))
await read(heal, 0, 10)
console.warn = warn
assert.equal(healReads, 1, 'короткий блок из кэша перекачан из сети')
console.log('PDF short cached chunk OK')

// #2038: чтение из кэша не ждёт очередь записей (она перебирает весь кэш).
let hangKeys = false
globalThis.caches = { open: async () => ({
  keys: async () => hangKeys ? new Promise(() => {}) : [...entries.keys()].map(url => ({ url })),
  match: async key => entries.get(key.url || key)?.clone(),
  put: async (key, value) => entries.set(key, value.clone()),
  delete: async key => entries.delete(key.url || key),
}) }
const warm = await openPDFSource('/heal', {PDFDataRangeTransport:Transport})
hangKeys = true
const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('чтение из кэша ждёт очередь записей')), 1000))
await Promise.race([read(warm, 0, 10), timeout])
assert.equal(healReads, 1, 'тёплый кэш читается без сети')
console.log('PDF cache read does not wait writes OK')
process.exit(0)
