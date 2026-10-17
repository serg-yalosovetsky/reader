// Ленивое открытие EPUB по HTTP Range (serg/tasks#1966): файл книги не
// скачивается целиком — ZipReader читает по сети каталог zip и только те
// записи (секции, стили, картинки), которые foliate запрашивает по мере
// листания. Требует от сервера 206 на Range (FileResponse умеет).

const TRANSIENT = new Set([429, 502, 503, 504])
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// Reader для @zip.js: достаточно size, readUint8Array(offset, length) и
// readable — ZipReader сам находит EOCD и центральный каталог, читая их
// по кускам, а данные записей гонит через readable-поток.
export class HttpRangeReader {
  constructor(url, size) {
    this.url = url
    this.size = size
    this.chunkSize = 65536
  }
  async readUint8Array(offset, size, attempt = 0) {
    const end = Math.min(offset + size, this.size) - 1
    let r
    try {
      r = await fetch(this.url, {
        headers: { Range: `bytes=${offset}-${end}` },
        credentials: 'same-origin', cache: 'no-store',
      })
    } catch (e) {
      if (attempt >= 3) throw e
      await sleep(400 * 2 ** attempt)
      return this.readUint8Array(offset, size, attempt + 1)
    }
    if (r.status === 206) return new Uint8Array(await r.arrayBuffer())
    // Сервер/прокси без Range отдаёт всё тело: раз байты уже здесь, срезаем
    // нужное (но только с начала файла — иначе каждый вызов качал бы всё).
    if (r.ok && offset === 0) return new Uint8Array(await r.arrayBuffer()).slice(0, end + 1)
    try { await r.body?.cancel() } catch {}
    const e = new Error(`range read: ${r.status}`)
    e.status = r.status
    if (!TRANSIENT.has(r.status) || attempt >= 3) throw e
    await sleep(400 * 2 ** attempt)
    return this.readUint8Array(offset, size, attempt + 1)
  }
  // Поток данных записи: ZipReader перед usage ставит на стрим offset/size
  // (границы записи в файле), pull читает их кусками по chunkSize.
  get readable() {
    const reader = this
    const stream = new ReadableStream({
      start() { this.chunkOffset = 0 },
      async pull(controller) {
        // Границы записи zip.js ставит на сам стрим (Object.assign на стриме
        // в его getData), chunkOffset живёт на source-объекте.
        const { offset = 0, size, diskNumberStart } = stream
        const remaining = size == null
          ? reader.chunkSize
          : Math.min(reader.chunkSize, size - this.chunkOffset)
        const chunk = await reader.readUint8Array(offset + this.chunkOffset, remaining)
        if (diskNumberStart) chunk.diskNumberStart = diskNumberStart
        controller.enqueue(chunk)
        if (this.chunkOffset + remaining >= (size ?? Infinity)
            || (size == null && !chunk.length && remaining === reader.chunkSize)) controller.close()
        else this.chunkOffset += reader.chunkSize
      },
    })
    return stream
  }
}

// Проба одним крошечным запросом: умеет ли сервер 206 и какой это файл.
// Заодно достаём размер и SHA-1 (book_rev) из заголовков.
export async function probeRangeSource(url) {
  let probe
  try {
    probe = await fetch(url, {
      headers: { Range: 'bytes=0-511' },
      credentials: 'same-origin', cache: 'no-store',
    })
  } catch { return null }
  try {
    if (probe.status !== 206) return null
    const m = /bytes\s+0-\d+\/(\d+)\s*$/.exec(probe.headers.get('Content-Range') || '')
    if (!m || !(+m[1])) return null
    return {
      url,
      size: +m[1],
      format: probe.headers.get('X-Book-Format') || '',
      sha1: probe.headers.get('X-Book-SHA1') || '',
    }
  } finally {
    try { await probe.body?.cancel() } catch {}
  }
}

// Собирает foliate-книгу из EPUB, лежащего на сервере, без полной закачки.
export async function makeEpubRange({ url, size }) {
  const { makeZipLoader } = await import('../../vendor/foliate-js/view.js')
  const { EPUB } = await import('../../vendor/foliate-js/epub.js')
  const loader = await makeZipLoader(new HttpRangeReader(url, size))
  return new EPUB(loader).init()
}
