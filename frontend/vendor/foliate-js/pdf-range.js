// Фиксированные блоки позволяют повторно использовать пересекающиеся запросы pdf.js.
export const CHUNK_SIZE = 512 * 1024
export const CHUNKS_CACHE = 'reader-pdf-chunks-v1'
export const PREFETCH_PAGES = 3
export const chunkIndices = (begin, end, size = CHUNK_SIZE) =>
    Array.from({ length: Math.max(0, Math.ceil(end / size) - Math.floor(begin / size)) }, (_, i) => Math.floor(begin / size) + i)
export function joinChunks(chunks, begin, end, size = CHUNK_SIZE) {
    const result = new Uint8Array(end - begin)
    let offset = 0
    for (const chunk of chunks) {
        const start = offset === 0 ? begin % size : 0
        const part = chunk.subarray(start, start + result.length - offset)
        result.set(part, offset); offset += part.length
    }
    return result
}
export function prefetchPages(index, count, radius = PREFETCH_PAGES) {
    return Array.from({ length: radius }, (_, i) => [index + i + 1, index - i - 1])
        .flat().filter(i => i >= 0 && i < count)
}
export function chunkVictims(entries, incoming, limit) {
    let bytes = entries.reduce((n, e) => n + e.size, incoming)
    return [...entries].sort((a, b) => a.used - b.used).filter(e => {
        if (bytes <= limit) return false
        bytes -= e.size; return true
    }).map(e => e.key)
}
export function checkPDFResponse(response) {
    if (response.type === 'opaqueredirect' || response.redirected || [401, 403].includes(response.status) || (response.status >= 300 && response.status < 400) || /html/.test(response.headers.get('Content-Type') || ''))
        throw new Error('Нужно войти заново, чтобы открыть PDF')
    if (!response.ok) throw new Error(`Не удалось загрузить PDF: ${response.status}`)
}
let writes = Promise.resolve()
export async function openPDFSource(source, lib) {
    const url = new URL(typeof source === 'string' ? source : source.url, location.href).href
    const head = source.response ?? await fetch(url, { method: 'HEAD', credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
    let length, etag, rev
    let first
    if (head.status === 206) {
        first = head
        checkPDFResponse(first)
        length = Number(first.headers.get('Content-Range')?.match(/\/(\d+)$/)?.[1])
        etag = first.headers.get('ETag'); rev = first.headers.get('X-Book-SHA1')
    } else if (head.ok) {
        checkPDFResponse(head)
        length = Number(head.headers.get('Content-Length'))
        etag = head.headers.get('ETag'); rev = head.headers.get('X-Book-SHA1')
    } else if (![405, 501].includes(head.status)) checkPDFResponse(head)
    if (!length) {
        first = await fetch(url, { headers: { Range: `bytes=0-${CHUNK_SIZE - 1}` }, credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
        checkPDFResponse(first)
        length = Number(first.headers.get('Content-Range')?.match(/\/(\d+)$/)?.[1])
        etag = first.headers.get('ETag'); rev = first.headers.get('X-Book-SHA1')
        if (first.status !== 206 || !length) { await first.body?.cancel(); throw new Error('Сервер не поддерживает частичную загрузку PDF') }
    }
    let cache
    try {
        cache = await caches.open(CHUNKS_CACHE)
        for (const req of await cache.keys()) {
            const key = new URL(req.url)
            if (key.searchParams.get('book') === url && key.searchParams.get('etag') !== (etag || rev || '')) await cache.delete(req)
        }
    } catch { /* Приватный режим: читаем без постоянного кэша. */ }
    const keyFor = index => {
        const key = new URL('/__reader_pdf_chunk__', location.href)
        key.search = new URLSearchParams({ book: url, etag: etag || rev || '', chunk: index })
        return key.href
    }
    const put = (key, bytes) => {
        const task = async () => {
            if (!cache || !(etag || rev)) return
            try {
                const entries = await Promise.all((await cache.keys()).map(async req => {
                    const r = await cache.match(req)
                    return { key: req.url, size: Number(r.headers.get('X-Size')), used: Number(r.headers.get('X-Used')) }
                }))
                const estimate = await navigator.storage?.estimate?.()
                const limit = Math.min(300 * 1048576, estimate?.quota ? estimate.quota * .2 : Infinity)
                const others = entries.filter(e => e.key !== key)
                for (const victim of chunkVictims(others, bytes.length, limit)) await cache.delete(victim)
                if (bytes.length > limit || (estimate?.quota && estimate.usage + bytes.length > estimate.quota * .95)) return
                await cache.put(key, new Response(bytes, { headers: { 'X-Size': bytes.length, 'X-Used': Date.now() } }))
            } catch { /* Квота не мешает чтению. */ }
        }
        writes = writes.then(task, task)
        return writes
    }
    const controllers = new Set(), pending = new Map()
    let stopped = false, fail = () => {}
    const chunk = index => {
        if (pending.has(index)) return pending.get(index)
        const job = (async () => {
            const key = keyFor(index)
            const hit = (etag || rev) ? await cache?.match(key).catch(() => null) : null
            if (hit) {
                const bytes = new Uint8Array(await hit.arrayBuffer())
                await put(key, bytes)
                return bytes
            }
            if (stopped) throw new Error('PDF закрыт')
            const controller = new AbortController(); controllers.add(controller)
            try {
                const begin = index * CHUNK_SIZE, end = Math.min(length, begin + CHUNK_SIZE)
                const response = index === 0 && first ? first : await fetch(url, {
                    headers: { Range: `bytes=${begin}-${end - 1}`, ...(etag ? { 'If-Range': etag } : {}) },
                    credentials: 'same-origin', redirect: 'manual', cache: 'no-store', signal: controller.signal,
                })
                if (index === 0) first = null
                checkPDFResponse(response)
                const range = response.headers.get('Content-Range')
                if (response.status !== 206 || range !== `bytes ${begin}-${end - 1}/${length}` || (etag && response.headers.get('ETag') !== etag)) {
                    await response.body?.cancel()
                    // Не смешиваем объекты разных редакций в одном документе.
                    if (cache) for (const req of await cache.keys()) if (new URL(req.url).searchParams.get('book') === url) await cache.delete(req)
                    throw new Error('PDF изменился на сервере. Откройте книгу заново')
                }
                const bytes = new Uint8Array(await response.arrayBuffer())
                if (bytes.length !== end - begin) throw new Error('Неполный блок PDF')
                await put(key, bytes)
                return bytes
            } finally { controllers.delete(controller) }
        })()
        pending.set(index, job)
        job.finally(() => pending.delete(index)).catch(() => {})
        return job
    }
    const transport = new lib.PDFDataRangeTransport(length, new Uint8Array())
    transport.requestDataRange = (begin, end) => {
        Promise.all(chunkIndices(begin, end).map(chunk)).then(parts => {
            if (!stopped) transport.onDataRange(begin, joinChunks(parts, begin, end))
        }).catch(error => { if (!stopped) fail(error) })
    }
    transport.abort = () => { stopped = true; for (const c of controllers) c.abort() }
    return { transport, rev, onError: callback => { fail = callback } }
}
