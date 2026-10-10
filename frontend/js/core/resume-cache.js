import { inlineImagesOnLoad } from './inline-images.js'
// Persistent, already laid-out resume pages. The live reader warms a separate
// view; it never moves the visible reader to produce the ±10 page window.
const DB = 'reader-resume-v1'
const STORE = 'windows'
const RADIUS = 10
const MAX_BYTES = 12 * 1024 * 1024
const MAX_BOOKS = 3
let dbPromise
let active = null
let warmTimer
let generation = 0
let warmer = null

export function resumeKey(work, prefs, host, original = false) {
  return JSON.stringify([2, String(work.id), work.content_updated_at || '',
    work.chapters_count || 0, original, prefs.theme, prefs.fontFamily,
    prefs.fontScale, prefs.marginLevel, prefs.flow, prefs.columns || 1,
    Math.round(host.clientWidth), Math.round(host.clientHeight)])
}

function database() {
  if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: 'id' })
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  return dbPromise
}
async function read(id) {
  const db = await database()
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE).objectStore(STORE).get(String(id))
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}
async function write(record) {
  // A huge illustrated section must not grow the resume cache without bound.
  record.bytes = JSON.stringify(record).length * 2
  if (record.bytes > MAX_BYTES) return
  const db = await database()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    const store = tx.objectStore(STORE)
    store.put(record)
    const req = store.getAll()
    req.onsuccess = () => {
      const records = req.result.sort((a, b) => b.savedAt - a.savedAt)
      let bytes = 0
      records.forEach((r, i) => {
        bytes += r.bytes || 0
        if (i >= MAX_BOOKS || bytes > MAX_BYTES) store.delete(r.id)
      })
    }
    tx.oncomplete = resolve
    tx.onerror = () => reject(tx.error)
  })
}

const asDataUrl = blob => new Promise((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(reader.result)
  reader.onerror = () => reject(reader.error)
  reader.readAsDataURL(blob)
})

// Blob URLs cease to exist after reload. Inline book styles/images and the
// selected font while warming, never while opening a cached page.
async function frozenDocument(doc, assets) {
  const root = doc.documentElement.cloneNode(true)
  root.querySelectorAll('script, base, iframe, object, embed, form').forEach(el => el.remove())
  // Missing/oversized image assets still keep their original occupied space.
  const images = [...doc.querySelectorAll('img')]
  root.querySelectorAll('img').forEach((el, i) => {
    const style = doc.defaultView.getComputedStyle(images[i])
    el.style.setProperty('width', style.width, 'important')
    el.style.setProperty('height', style.height, 'important')
    el.loading = 'eager'
  })
  for (const el of [root, ...root.querySelectorAll('*')]) {
    for (const attr of [...el.attributes]) {
      if (/^on/i.test(attr.name)) el.removeAttribute(attr.name)
    }
    if (el.matches('a')) el.removeAttribute('href')
  }
  async function fetchAsset(url, css = false, depth = 0) {
    if (depth > 3) return ''
    const key = `${css}:${url}`
    if (!assets.has(key)) assets.set(key, (async () => {
      try {
        const resp = await fetch(url, { signal: AbortSignal.timeout(2500) })
        if (!resp.ok) return ''
        if (css) return await freezeCSS(await resp.text(), url, depth + 1)
        const blob = await resp.blob()
        return blob.size <= 2 * 1024 * 1024 ? await asDataUrl(blob) : ''
      } catch { return '' }
    })())
    return assets.get(key)
  }
  async function freezeCSS(css, base, depth = 0) {
    for (const m of [...css.matchAll(/@import\s+url\(['"]?([^'"\s)]+)['"]?\)[^;]*;/g)]) {
      const url = new URL(m[1], base).href
      css = css.replace(m[0], await fetchAsset(url, true, depth))
    }
    for (const m of [...css.matchAll(/url\(['"]?([^'"\s)]+)['"]?\)/g)]) {
      if (/^(data:|#)/.test(m[1])) continue
      const url = new URL(m[1], base).href
      css = css.replace(m[0], `url("${await fetchAsset(url, false, depth)}")`)
    }
    return css
  }
  await Promise.all([...root.querySelectorAll('link[rel="stylesheet"]')].map(async el => {
    const style = doc.createElement('style')
    style.textContent = await fetchAsset(el.href, true)
    el.replaceWith(style)
  }))
  await Promise.all([...root.querySelectorAll('style')].map(async el => {
    el.textContent = await freezeCSS(el.textContent, doc.baseURI)
  }))
  await Promise.all([...root.querySelectorAll('img[src]')].map(async el => {
    if (!el.src.startsWith('data:')) {
      el.src = await fetchAsset(el.src)
      el.removeAttribute('srcset')
    }
  }))
  let html = root.outerHTML
  const urls = [...new Set(html.match(/blob:[^\s"'<>)]*/g) || [])]
  for (const url of urls) html = html.split(url).join(await fetchAsset(url))
  // Cached documents are inert, self-contained and cannot initiate requests.
  html = html.replace(/<head([^>]*)>/i, `<head$1><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' data:; img-src data:; font-src data:">`)
  return '<!doctype html>' + html
}

function position(location) {
  return { locator: location?.cfi || '', ratio: location?.fraction || 0,
    text_anchor: (location?.range?.toString() || '').replace(/\s+/g, ' ').trim().split(' ').slice(0, 12).join(' '),
    chapter: location?.tocItem?.label || '' }
}

async function snapshot(view, record, assets) {
  const s = view.renderer?.snapshot?.()
  if (!s || !view.lastLocation?.cfi) return null
  // One serialized document per section, rather than 21 copies of a chapter.
  const key = String(s.index)
  if (!record.documents[key]) record.documents[key] = await frozenDocument(s.doc, assets)
  return { document: key, offset: s.offset, clip: s.clip, frame: s.frame, position: position(view.lastLocation) }
}

function showPage() {
  const { record, index, host, onPosition } = active
  const page = record.pages[index]
  const layer = document.createElement('div')
  layer.className = 'resume-page'
  layer.setAttribute('aria-label', 'Страница книги')
  const clip = document.createElement('div')
  clip.className = 'resume-clip'
  const frame = document.createElement('iframe')
  frame.title = page.position.chapter || 'Страница книги'
  frame.setAttribute('sandbox', 'allow-same-origin')
  frame.setAttribute('scrolling', 'no')
  const rect = (el, r) => Object.assign(el.style, {
    left: `${r.x}px`, top: `${r.y}px`, width: `${r.width}px`, height: `${r.height}px`,
  })
  rect(clip, page.clip); rect(frame, page.frame)
  // Keep the old page until its replacement has painted.
  frame.addEventListener('load', () => {
    if (!active || active.record !== record || active.index !== index) { layer.remove(); return }
    for (const old of host.querySelectorAll('.resume-page')) if (old !== layer) old.remove()
    const doc = frame.contentDocument
    doc.addEventListener('keydown', e => {
      if (['ArrowRight', 'ArrowDown', 'PageDown', ' ', 'Enter'].includes(e.key)) { e.preventDefault(); turnResume(e.shiftKey ? -1 : 1) }
      if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(e.key)) { e.preventDefault(); turnResume(-1) }
    })
    doc.addEventListener('wheel', e => { e.preventDefault(); turnResume(e.deltaY > 0 ? 1 : -1) }, { passive: false })
    let start
    doc.addEventListener('touchstart', e => { const t = e.touches[0]; start = [t.clientX, t.clientY] }, { passive: true })
    doc.addEventListener('touchend', e => {
      if (!start) return
      const t = e.changedTouches[0], dx = t.clientX - start[0], dy = t.clientY - start[1]
      if (Math.max(Math.abs(dx), Math.abs(dy)) > 35) turnResume(Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? 1 : -1) : (dy < 0 ? 1 : -1))
      start = null
    }, { passive: true })
    host.dataset.resumeVisible = 'true'
  }, { once: true })
  frame.srcdoc = record.documents[page.document]
  clip.append(frame); layer.append(clip); host.append(layer)
  onPosition?.(page.position, false, page)
}

export async function showResume(work, prefs, host, opts = {}, onPosition) {
  if (opts.jump || opts.original) return false
  const token = generation
  try {
    const record = await read(work.id)
    if (token !== generation) return false
    if (!record || record.key !== resumeKey(work, prefs, host) || !record.pages[record.center]) return false
    active = { record, work, prefs, index: record.center, host, onPosition, base: record.pages[record.center].position }
    showPage()
    return true
  } catch { return false }
}
export function resumePosition() { return active?.record.pages[active.index]?.position || null }
export function resumeBasePosition() { return active?.base || null }
export function turnResume(dir) {
  if (!active) return false
  if (active.record.key !== resumeKey(active.work, active.prefs, active.host)) {
    hideResume(); return false
  }
  const index = active.index + dir
  if (active.record.pages[index]) {
    active.index = index
    showPage()
    active.onPosition?.(resumePosition(), true, active.record.pages[active.index])
    // Remember a cached turn even if the tab closes before the live file loads.
    active.record.center = index
    active.record.savedAt = Date.now()
    write(active.record).catch(() => {})
  }
  return true // at the window boundary, wait for the live reader
}
export function hideResume() {
  if (!active) return
  active.host.querySelectorAll('.resume-page').forEach(el => el.remove())
  delete active.host.dataset.resumeVisible
  active = null
}

function cancelWarm() {
  generation++
  clearTimeout(warmTimer)
  if (warmer) { try { warmer.close() } catch {} warmer.remove(); warmer = null }
}
export function stopResume() { cancelWarm(); hideResume() }

// Called after visible relocations. Save the current page first; grow the window
// in idle time. Abandon work as soon as the user moves or closes the book.
export function scheduleResume(view, work, prefs, host, applyStyles, original = false) {
  cancelWarm()
  if (!view.renderer?.snapshot || !view.lastLocation?.cfi || original) return
  const token = generation
  const run = async () => {
    let worker
    const valid = () => generation === token && view.isConnected
    const record = { id: String(work.id), key: resumeKey(work, prefs, host),
      savedAt: Date.now(), pages: [], documents: {}, center: 0 }
    const assets = new Map()
    try {
      const first = await snapshot(view, record, assets)
      if (!first || !valid()) return
      record.pages = [first]
      await write(record)
      if (!valid()) return
      worker = document.createElement('foliate-view')
      warmer = worker
      worker.className = 'resume-warmer'
      worker.setAttribute('aria-hidden', 'true')
      Object.assign(worker.style, { width: `${host.clientWidth}px`, height: `${host.clientHeight}px` })
      document.body.append(worker)
      worker.addEventListener('load', inlineImagesOnLoad)
      // Reuse the parsed book, with a read-only lifetime. Loading a chapter may
      // share its blob URL, but this renderer must never unload/destroy live
      // resources. Bind prototype methods to the real book (private fields).
      // This avoids unpacking a large EPUB again on every visible page turn.
      const sections = view.book.sections.map(section => ({ ...section, unload() {} }))
      const book = new Proxy(view.book, { get(target, property) {
        if (property === 'sections') return sections
        if (property === 'transformTarget') return undefined
        if (property === 'destroy') return () => {}
        const value = Reflect.get(target, property, target)
        return typeof value === 'function' ? value.bind(target) : value
      } })
      await worker.open(book)
      if (!valid()) return
      applyStyles(worker)
      await worker.init({ lastLocation: first.position.locator })
      if (!valid()) return
      // Let fonts settle once so neighbouring pages use the live layout.
      const settle = async () => {
        const doc = worker.renderer.getContents()[0]?.doc
        if (doc?.fonts?.ready) await Promise.race([doc.fonts.ready, new Promise(r => setTimeout(r, 2500))])
        // Paginator coalesces ResizeObserver renders for 80ms. A freshly
        // loaded section must finish that render before recording/turning it.
        await new Promise(r => setTimeout(r, 120))
      }
      await settle()
      if (!valid()) return
      await worker.goTo(first.position.locator)
      await worker.renderer.restoreSnapshot(first)
      const center = await snapshot(worker, record, assets)
      const before = [], after = []
      for (const [dir, pages] of [[-1, before], [1, after]]) {
        await worker.goTo(first.position.locator)
        await settle()
        if (!valid()) return
        await worker.renderer.restoreSnapshot(first)
        for (let n = 0; n < RADIUS && valid(); n++) {
          const previous = worker.lastLocation?.cfi
          const distance = worker.renderer.scrolled ? Math.round(worker.renderer.size * 0.9) : undefined
          await (dir < 0 ? worker.prev(distance) : worker.next(distance))
          await settle()
          if (!valid() || worker.lastLocation?.cfi === previous) break
          const page = await snapshot(worker, record, assets)
          if (!page) break
          pages.push(page)
          await new Promise(r => setTimeout(r, 0))
        }
      }
      if (!valid()) return
      record.pages = [...before.reverse(), center || first, ...after]
      record.center = before.length
      record.savedAt = Date.now()
      await write(record)
    } catch (e) { console.debug('resume cache unavailable', e.message) }
    finally {
      if (worker) { try { worker.close() } catch {} worker.book?.destroy?.(); worker.remove() }
      if (warmer === worker) warmer = null
    }
  }
  warmTimer = setTimeout(() => {
    if (window.requestIdleCallback) requestIdleCallback(() => { if (validToken()) run() }, { timeout: 1500 })
    else run()
  }, 450)
  const validToken = () => generation === token
}

window.addEventListener('resize', () => {
  if (active && active.record.key !== resumeKey(active.work, active.prefs, active.host)) hideResume()
})
