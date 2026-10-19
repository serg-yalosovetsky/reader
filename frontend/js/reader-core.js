import { protectRemoteImages, restoreRemoteImages } from './core/remote-images.js'
import { checkPDFResponse } from '../vendor/foliate-js/pdf-range.js'
// Ядро читалки: открытие книги, релокейт/прогресс, стили книги, TOC.
import { setMenuFormat } from './menu-autohide.js'
import { $, toast } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { prefs, MARGIN_INLINE, MARGIN_SIDE_PCT, MARGIN_GAP, FONT_STACKS,
         gfontsFor } from './core/prefs.js'
import { view, currentWork, lastCfi, libMonitored, libUpdated, navStack,
         setView, setCurrentWork, setLastCfi, setLastAnchor, setLastIdx,
         setLastFraction } from './core/state.js'
import { restorePosition, captureAnchor, gotoPosition, bookRev, positionForFormat } from './core/position.js'
import { commitPosition, pendingFor, serverSavedAt, replayPending } from './core/progress-sync.js'
import { inlineImagesOnLoad } from './core/inline-images.js'
import { ttsSt, ttsStop, ttsReadPage } from './tts.js'
import { loadHighlightsWeb, onDrawAnnotation, hideSelPopup } from './highlights.js'
import { attachKeysToDoc, closePanels } from './navigation.js'
import { recentBook, storeRecent, changedFile, readWithProgress, fileSource } from './core/recent-books.js'
import { cachedBook } from './core/offline.js'
import { probeRangeSource, makeEpubRange } from './core/book-range.js'
import { updateProgress, buildChapterMarks } from './progress-bar.js'
import { setBookMeta, setChapterTitle, setMoreBadge } from './chrome.js'
import { onTranslateRelocate, resetTranslate } from './translate.js'
import { readerUrl } from './core/reader-url.js'
import { convertible, pdfAsEpub, ensureEpub, convertStatus, formatSwitchTarget, readingFormat, formatRecord, newerReadingFormat, localReadingFormat, storeReadingFormat } from './core/convert.js'
import { resetJumps, recordJump, pushServerJump, noteProgressSaved } from './jumps.js'
import { collapse } from '../vendor/foliate-js/epubcfi.js'
import { chapterDate } from './core/chapterdate.js'
import { showResume, hideResume, stopResume, resumePosition, resumeBasePosition, layoutSettled,
         scheduleResume, resumeKey, resumeBaseSavedAt } from './core/resume-cache.js'

// ===================== ЧИТАЛКА =====================
let saveTimer = null
let openGeneration = 0
let opening = false
let openedFile = null
let originalFile = false

// Общий путь открытия сохраняет позицию и переносит долю между форматами.
let formatTarget = null
function paintFormatSwitch(epubReady = false) {
  const btn = $('#reader-format-btn')
  formatTarget = formatSwitchTarget(currentWork, currentFormat, epubReady)
  btn.hidden = !formatTarget
  btn.textContent = formatTarget === 'epub' ? 'EPUB' : (formatTarget || '').toUpperCase()
  btn.title = formatTarget === 'epub' ? 'Читать EPUB-версию' : 'Читать оригинал файла'
  btn.setAttribute('aria-label', btn.title)
}
document.addEventListener('reader-format-change', () => {
  paintFormatSwitch()
  if (!convertible(currentWork) || !currentFormat || currentFormat === 'epub') return
  const generation = openGeneration
  convertStatus(currentWork.id).then(st => {
    if (generation === openGeneration) paintFormatSwitch(!!st.ready)
  }).catch(e => logErr('format switch status', e))
})
$('#reader-format-btn').addEventListener('click', () => {
  if (!formatTarget || !currentWork) return
  openReader(currentWork, formatTarget === 'epub' ? { fmt: 'epub' } : { original: true })
})

// Якоря одной позиции: старые записи (кеш, сервер) хранят 12 слов, новые — 24.
function sameAnchor(a, b) {
  return !a || !b || a.startsWith(b) || b.startsWith(a)
}
// Перезапуск сервиса (деплой) — ~10 с ответов 502 от nginx. Книга, открытая в
// это окно, падала «Не удалось открыть книгу: 502», а хуже — без ответа на
// запрос позиции открывалась с начала и первым же релокейтом затирала место.
// Повторяем временные отказы ~20 с (0.5+1+2+4×4).
const TRANSIENT = new Set([502, 503, 504])
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function withRetry(attempt, isTransient, alive) {
  let delay = 500
  for (let i = 0; ; i++) {
    try { return await attempt() } catch (e) {
      if (i >= 7 || !isTransient(e) || !alive()) throw e
    }
    await sleep(delay)
    delay = Math.min(delay * 2, 4000)
  }
}
const transientErr = (e) => e?.name === 'NetworkError' || TRANSIENT.has(e?.status)
async function fetchBookFile(url, alive, headers = {}) {
  return withRetry(async () => {
    const r = await fetch(url, { headers, cache: 'no-store' })
    if (TRANSIENT.has(r.status)) { const e = new Error(String(r.status)); e.status = r.status; throw e }
    return r
  }, (e) => TRANSIENT.has(e?.status) || e instanceof TypeError, alive)
}
// Серверная позиция НЕ пришла, книга открыта с начала: пока человек сам
// ничего не сделал в читалке, ничего не пишем — иначе начало книги затёрло бы
// сохранённое место. Ждём именно действия человека: CFI и доля после открытия
// меняются и без него (раскладка, переход к началу текста).
let holdSaves = false
const USER_INPUT = ['pointerdown', 'keydown', 'wheel', 'touchstart']
function releaseHold() { holdSaves = false }
for (const ev of USER_INPUT) window.addEventListener(ev, releaseHold, { capture: true, passive: true })

// Формат и SHA-1 открытого файла: к ним привязан сохраняемый CFI.
export let currentFormat = ''
let currentRev = ''
// Листали страницы из кеша, пока живая книга грузилась.
let cachedTurned = false

const progressWrites = new Map()
// Позиция уходит через очередь (core/progress-sync.js): сперва localStorage,
// потом PUT. Записи одной книги идут строго по очереди.
function savePosition(pos, opts = {}) {
  if (!currentWork || !pos) return
  if (holdSaves) return
  const id = currentWork.id
  const body = { book_rev: currentRev, file_format: currentFormat, ...pos }
  const savedAt = Date.now()
  if (opts.keepalive) { commitPosition(id, body, { ...opts, savedAt }); return }
  const write = (progressWrites.get(id) || Promise.resolve())
    .then(() => commitPosition(id, body, { ...opts, savedAt }))
  progressWrites.set(id, write)
  write.finally(() => { if (progressWrites.get(id) === write) progressWrites.delete(id) })
}
// Показана ли последняя страница книги. Пагинатор (EPUB/FB2) знает это сам —
// renderer.atEnd: нет следующей линейной секции и страница последняя. У PDF
// (fixed-layout) атрибута нет, но там страница = секция.
function isAtEnd(detail) {
  const r = view?.renderer
  if (r && 'atEnd' in r) return !!r.atEnd
  const sec = detail?.section
  return !!sec && typeof sec.current === 'number' && sec.total > 0 && sec.current >= sec.total - 1
}
// Текущая позиция живой читалки — единственный источник, из которого пишем.
function livePosition() {
  const loc = view?.lastLocation
  if (!loc) return null
  return { ratio: loc.fraction || 0, locator: loc.cfi || '',
    text_anchor: captureAnchor(loc.range), chapter: loc.tocItem?.label || '',
    at_end: isAtEnd(loc) }
}
// Немедленно записать место (закрытие книги, уход вкладки в фон).
function flushPosition(opts = {}) {
  clearTimeout(saveTimer)
  if (opening) {
    // Живая книга ещё не встала: писать есть что, только если листали кеш.
    // replay — сервер примет, лишь если на нём позиция не новее.
    const cached = resumePosition()
    if (cached && cachedTurned) savePosition({ ...cached, book_rev: '' }, { ...opts, replay: true })
    return
  }
  const pos = livePosition()
  if (pos) savePosition(pos, opts)
}
export function disposeReader() {
  if (currentWork) flushPosition()
  cachedTurned = false
  openGeneration++
  stopResume()
  try { view?.close() } catch {}
  view?.book?.destroy?.()
  openedFile = null
}

// opts.original=true — открыть исходный файл (PDF как макет), минуя EPUB-версию.
export async function openReader(work, opts = {}) {
  const attempt = openGeneration + 1
  try { await openReaderImpl(work, opts) }
  catch (e) {
    if (attempt !== openGeneration) return
    logErr('open reader', e)
    toast(resumePosition() ? 'Показываю сохранённые страницы. Книга пока не загрузилась.' : 'Не удалось открыть книгу: ' + e.message, 'err', 6000)
  }
}
async function openReaderImpl(work, opts = {}) {
  ttsStop()
  disposeReader()
  const generation = openGeneration
  opening = true
  originalFile = false
  const current = () => generation === openGeneration
  // Без file_format читалка не узнаёт PDF: не помнит последний формат (#1937)
  // и не показывает кнопку PDF. Его нет в строке списка, закэшированной до
  // того, как список стал его отдавать, — тогда берём деталь книги.
  if (work.file_format == null) {
    try { work = { ...work, ...await api.get(`/api/library/${work.id}`) } }
    catch (e) { logErr('load work format', e) }
    if (!current()) return
  }
  setCurrentWork(work)
  currentRev = ''
  currentFormat = ''
  setMenuFormat('')
  document.dispatchEvent(new Event('reader-format-change'))
  setLastIdx(null); setLastCfi(''); setLastAnchor('')
  document.body.classList.add('reader-open')
  $('#library').hidden = true
  $('#reader').hidden = false
  // Прокрутка библиотеки/страницы книги не должна срезать верх читалки: у
  // body.reader-open прокрутки нет, и сдвиг оставался навсегда (serg/tasks#902).
  window.scrollTo(0, 0)
  // Заголовок сверху: по умолчанию — название главы (ставится первым
  // relocate), по тапу переключается на книгу и автора.
  setBookMeta(work)
  resetTranslate()
  const updBtn = $('#update-btn')
  // Кнопка нужна и книге без подписки, если у неё есть адрес на сайте: нажатие
  // поставит её на отслеживание (navigation.js). Иначе такую книгу обновить
  // из читалки было нечем (serg/tasks#1853).
  updBtn.hidden = !libMonitored.has(work.id) && !/^https?:\/\//.test(work.source_url || '')
  updBtn.dataset.state = ''
  updBtn.disabled = false
  updBtn.title = 'Проверить новые главы'
  { const _st = $('#update-state'); if (_st) _st.textContent = '' }
  setMoreBadge('')

  // Позицию запрашиваем сразу, но локальный режим открывает кеш без сети.
  holdSaves = false
  const progP = (progressWrites.get(work.id) || Promise.resolve())
    .then(() => withRetry(() => api.get(`/api/progress/${work.id}`), transientErr, current))
    .catch(e => { logErr('load progress', e); return null })
  let localFormat = localReadingFormat(work)
  const pending = pendingFor(work.id)
  const pendingFormat = formatRecord(work, pending?.body)
  if (pendingFormat && (!localFormat || pendingFormat.saved_at > localFormat.saved_at)) {
    localFormat = pendingFormat
    storeReadingFormat(work, pendingFormat)
  }
  // Поздний ответ меняет только следующее открытие, если запись ещё новее.
  progP.then(prog => {
    const server = formatRecord(work, prog && { ...prog, saved_at: serverSavedAt(prog) })
    const local = localReadingFormat(work)
    const latest = newerReadingFormat(work, local, server)
    if (latest && (!local || latest.saved_at > local.saved_at)) storeReadingFormat(work, latest)
  }).catch(e => logErr('sync reading format', e))
  let savedProgress = null
  const explicitFormat = opts.original || opts.fmt === 'epub' || opts.fmt === String(work.file_format).toLowerCase()
  if (convertible(work) && !localFormat && !explicitFormat) {
    let timer
    try {
      savedProgress = await Promise.race([
        progP,
        new Promise(resolve => {
          timer = setTimeout(() => {
            logErr('load reading format', new Error('Таймаут 1500 мс: использую настройку формата'))
            resolve(null)
          }, 1500)
        }),
      ])
    } finally { clearTimeout(timer) }
  }
  if (!current()) return
  const selectedFormat = readingFormat(work, opts, localFormat, savedProgress, pdfAsEpub())
  if (convertible(work)) {
    opts = { ...opts, fmt: selectedFormat, original: selectedFormat !== 'epub' }
  }
  originalFile = !!opts.original

  // История/URL: URL отражает ОТКРЫТУЮ книгу (#read/<id>), чтобы при
  // переоткрытии вкладки читалка вернулась к той же книге (позицию хранит
  // сервер). Вход из библиотеки/страницы книги — новая запись (браузерный
  // «назад» → назад по сайту); переключение книги внутри читалки или
  // восстановление из URL — замена записи, чтобы не плодить историю.
  const bookHash = `#read/${encodeURIComponent(work.id)}`
  const requestedFormat = work.file_format === 'pdf' ? (opts.original ? 'pdf' : opts.fmt) : null
  const bookUrl = readerUrl(location.href, work.id, requestedFormat)
  const histState = { reader: true, id: work.id }
  if ((history.state && history.state.reader) || location.hash === bookHash) {
    history.replaceState(histState, '', bookUrl)
  } else {
    history.pushState(histState, '', bookUrl)
  }

  // Очистить прошлый экземпляр.
  $('#view-host').innerHTML = ''
  setView(document.createElement('foliate-view'))
  $('#view-host').append(view)
  const readerView = view

  // Загружаем файл как Blob → File с корректным именем (для детекта FB2).
  // Cache-first: если книга сохранена офлайн — читаем из кэша (без сети),
  // иначе тянем с сервера. Помогает на телефоне при плохом коннекте.
  let cachedTarget = null, cachedSnapshot = null, cachedMoved = false, cachedSave = null
  const cachedKey = resumeKey(work, prefs, $('#view-host'))
  await showResume(work, prefs, $('#view-host'), opts, (pos, moved, snapshot) => {
    cachedTarget = pos; cachedSnapshot = snapshot; cachedMoved ||= moved
    setLastCfi(pos.locator); setLastAnchor(pos.text_anchor); setLastFraction(pos.ratio)
    setChapterTitle(pos.chapter)
    updateProgress({ fraction: pos.ratio })
    // Перелистывание кеша НЕ пишем сразу: живой ответ сервера может оказаться
    // новее (дочитали на другом устройстве) — решаем, когда он придёт.
    if (moved) { cachedTurned = true; cachedSave = pos }
  })
  if (!current()) return
  const cachedBase = resumeBasePosition()
  let resp = await cachedBook(work.id)
  if (opts.original && resp?.headers.get('X-Book-Format') !== selectedFormat) resp = null
  // Явный EPUB не должен подхватить офлайн-копию исходного PDF.
  if (opts.fmt === 'epub' && resp?.headers.get('X-Book-Format') !== 'epub') resp = null
  if (!current()) return
  // PDF читаем как EPUB (перетекающий текст). Первое открытие ждёт конвертацию
  // (секунды–минуты на толстой книге), дальше файл готов и отдаётся сразу.
  let refreshed = null, fileCommitted = false
  let epubReady = false
  if (!resp && !opts.original && convertible(work) && (opts.fmt === 'epub' || pdfAsEpub())) {
    const t = toast('Перевожу в EPUB — будет перетекающий текст…', 'info', 60000)
    const st = await ensureEpub(work.id).catch(() => null)
    t?.close?.()
    epubReady = !!st?.ready
    if (!st?.ready) {
      toast(st?.error ? 'Не вышло сделать EPUB: ' + st.error : 'Конвертация ещё идёт — открываю оригинал', 'err', 6000)
    }
  }
  let rangeSource = null
  if (!resp) {
    const url = `/api/reader/${work.id}/file` + (opts.original ? '?original=1' : epubReady ? '?format=epub' : '')
    // HEAD выбирает тот же файл, что GET, включая готовую EPUB-версию PDF.
    if (opts.original || work.file_format === 'pdf') {
      let head = await fetch(url, { method: 'HEAD', credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
      if ([405, 501].includes(head.status)) head = await fetch(url, { headers: { Range: 'bytes=0-524287' }, credentials: 'same-origin', redirect: 'manual', cache: 'no-store' })
      if (head.ok) {
        checkPDFResponse(head)
        if ((head.headers.get('X-Book-Format') || (epubReady && !opts.original ? 'epub' : work.file_format)) === 'pdf') {
          rangeSource = { url, response: head, format: 'pdf',
            sha1: head.headers.get('X-Book-SHA1') || '' }
          resp = head
        }
      } else if (![405, 501].includes(head.status)) checkPDFResponse(head)
    }
    if (!rangeSource) resp = await recentBook(url)
    // Ленивое открытие EPUB: файл остаётся на сервере, браузер качает только
    // каталог zip и читаемые секции (HTTP Range). Офлайн-копия и кэш недавних
    // файлов идут прежним путём — там файл уже на устройстве.
    if (!resp && !rangeSource && !opts.original
        && (!work.file_format || work.file_format === 'epub'
            || (work.file_format === 'pdf' && epubReady))) {
      // Из строки списка библиотеки file_format не приходит — формат решает
      // сама проба: range открываем только если сервер отдал EPUB.
      rangeSource = await probeRangeSource(url)
      if (rangeSource && rangeSource.format !== 'epub') rangeSource = null
    }
    if (rangeSource && rangeSource.format !== 'pdf') { /* Секции загрузит ZipReader по Range. */ }
    else if (rangeSource) { /* Данные страниц загрузит PDFDataRangeTransport. */ }
    else if (fileSource(false, resp) === 'recent') {
      const cached = resp
      // Проверка сети не задерживает открытие и не прерывает чтение.
      fetchBookFile(url, current, cached.headers.get('ETag')
        ? { 'If-None-Match': cached.headers.get('ETag') } : {}).then(r => {
        if (changedFile(cached, r) && !r.redirected && !r.headers.get('Content-Type')?.includes('text/html'))
          { if (current() && !fileCommitted) refreshed = r.clone(); return storeRecent(url, r) }
      }).catch(() => {})
    } else {
      const indicator = toast('Загружаю книгу…', 'info', 3600000)
      try {
        resp = await readWithProgress(await fetchBookFile(url, current), (n, total) => {
          if (current() && indicator) indicator.textContent = total
            ? `Загружаю книгу… ${Math.min(100, Math.round(n / total * 100))}% (${(n / 1048576).toFixed(1)} из ${(total / 1048576).toFixed(1)} МБ)`
            : `Загружаю книгу… ${(n / 1048576).toFixed(1)} МБ`
        })
      } finally { indicator?.close?.() }
      if (resp.ok && !resp.redirected && !resp.headers.get('Content-Type')?.includes('text/html')) storeRecent(url, resp.clone())
    }
  }
  if (resp && !resp.ok) throw new Error('Не удалось открыть книгу: ' + resp.status)
  let blob = rangeSource ? null : await resp.blob()
  if (refreshed) { resp = refreshed; blob = await resp.blob() }
  fileCommitted = true
  if (!current()) return
  const revP = rangeSource ? Promise.resolve(rangeSource.sha1 || '') : bookRev(blob)
  // Формат берём из ответа: сервер мог отдать EPUB вместо PDF (имя файла решает,
  // каким движком foliate будет рендерить).
  const fmt = rangeSource?.format
    || resp?.headers?.get?.('X-Book-Format') || work.file_format || 'epub'
  if (work.file_format === 'pdf') {
    originalFile = fmt === 'pdf'
    history.replaceState(histState, '', readerUrl(location.href, work.id, fmt))
  }
  currentFormat = fmt
  storeReadingFormat(work, { file_format: fmt, saved_at: Date.now() })
  setMenuFormat(fmt)
  document.dispatchEvent(new Event('reader-format-change'))
  const name = `book.${fmt}`
  const file = !rangeSource ? new File([blob], name, { type: blob.type })
    : fmt === 'pdf' ? await (await import('../vendor/foliate-js/pdf.js')).makePDF(rangeSource)
    : await makeEpubRange(rangeSource)
  if (!current()) { if (rangeSource) await file.destroy(); return }
  if (rangeSource) file.onPDFError = error => toast(error.message, 'err', 10000)

  openedFile = file
  await readerView.open(file)
  if (!current()) { try { readerView.close() } catch {} readerView.book?.destroy?.(); return }
  const remoteImages = fmt !== 'pdf' && !view.isFixedLayout && protectRemoteImages(view.book)
  if (remoteImages) view.addEventListener('load', e => { if (e.detail?.doc) restoreRemoteImages(e.detail.doc) })
  navStack.length = 0
  document.getElementById('reader')?.classList.remove('chrome-hidden')
  view.addEventListener('relocate', e => { if (current() && !opening) onRelocate(e) })
  view.addEventListener('load', attachKeysToDoc)
  // Действия внутри книги (iframe) до окна не всплывают — слушаем и там.
  view.addEventListener('load', (e) => {
    for (const ev of USER_INPUT) e.detail?.doc?.addEventListener(ev, releaseHold, { capture: true, passive: true })
  })
  view.addEventListener('load', inlineImagesOnLoad)
  view.addEventListener('draw-annotation', onDrawAnnotation)
  currentRev = await revP
  applyViewStyles()
  buildTOC()
  loadTocDates(work.id)
  buildChapterMarks()

  // Восстановить позицию: текстовый якорь (устойчив к пересборке книги), иначе
  // CFI, иначе доля (напр. импорт из ReadEra), иначе начало. См. core/position.js.
  let prog = await progP
  currentRev = await revP
  if (!current()) return
  // Неотправленная позиция (закрыли без сети, PWA убили до PUT) новее
  // серверной — она и есть место, где остановились. Досылаем её же.
  const pend = pendingFor(work.id)
  if (pend?.body && pend.saved_at > serverSavedAt(prog)) {
    prog = { ...(prog || {}), ...pend.body }
    commitPosition(work.id, pend.body, { replay: true, savedAt: pend.saved_at })
  }
  const base = cachedBase
  // Более новая позиция с сервера (другое устройство) побеждает кеш страниц,
  // даже если его успели полистать: кеш старше — его листание тоже.
  const sameAsServer = prog && prog.locator && base && collapse(prog.locator) === collapse(base.locator)
    && sameAnchor(prog.text_anchor, base.text_anchor)
  const serverNewer = prog && serverSavedAt(prog) > resumeBaseSavedAt()
  const samePosition = base && positionForFormat(prog, fmt) === prog && fmt !== 'pdf' && (!prog || sameAsServer || (cachedMoved && !serverNewer))
  if (base && !samePosition) { hideResume(); cachedSave = null; cachedTurned = false }
  if (cachedSave) { savePosition({ ...cachedSave, book_rev: '' }); cachedSave = null }
  // Позиция, с которой книга открывается, — точка отсчёта для признака прыжка:
  // иначе первый же релокейт после восстановления выглядел бы скачком с нуля.
  setLastFraction(prog?.ratio || 0)
  await resetJumps(work.id, prog?.ratio || 0)
  if (!current()) return
  if (opts.jump) {
    // Пришли из оглавления на странице книги: открываем НЕ сохранённую
    // позицию, а выбранную главу. Сохранённую кладём в историю переходов —
    // иначе клик по главе стирал бы место, с которого человек читал.
    pushServerJump(prog, 'open')
    await openAtChapter(view, opts.jump)
  } else if (samePosition && cachedTarget) {
    await readerView.init({ lastLocation: cachedTarget.locator })
  } else {
    const restored = await restorePosition(readerView, prog, currentRev, fmt)
    // Неудачный переход не должен записать начало поверх сохранённой доли.
    if (!restored) holdSaves = true
  }
  if (!current()) return
  // The reader may have turned cached pages while the live view was loading.
  while (samePosition && cachedTarget && (readerView.lastLocation?.cfi && collapse(readerView.lastLocation.cfi)) !== collapse(cachedTarget.locator)) {
    const target = cachedTarget.locator
    await readerView.goTo(target)
    if (!current()) return
    if (cachedTarget?.locator === target) break
  }
  if (samePosition && cachedSnapshot && cachedKey === resumeKey(work, prefs, $('#view-host'))) {
    let target
    do {
      target = cachedTarget.locator
      await readerView.goTo(target)
      const doc = readerView.renderer.getContents?.()[0]?.doc
      if (doc?.fonts?.ready) await Promise.race([doc.fonts.ready, new Promise(r => setTimeout(r, 2500))])
      // Keep the cache visible through the paginator's 80ms resize debounce.
      await new Promise(r => setTimeout(r, 120))
      await layoutSettled(readerView)
      if (!current()) return
    } while (target !== cachedTarget.locator)
    await readerView.renderer.restoreSnapshot?.(cachedSnapshot)
    if (!current()) return
  }
  hideResume()
  // Позиция с сервера не пришла и неотправленной нет — место неизвестно.
  if (!prog && !opts.jump && !samePosition) {
    holdSaves = true
    toast('Сервер не отдал место чтения — открыл с начала. Сохранённое место не тронуто.', 'err', 6000)
  }
  opening = false
  cachedTurned = false
  if (readerView.lastLocation) onRelocate({ detail: readerView.lastLocation })

  // Подтянуть и нарисовать сохранённые подсветки (синк с сервером/Android).
  loadHighlightsWeb()
}

function onRelocate(e) {
  if (openedFile) scheduleResume(view, currentWork, prefs, $('#view-host'), applyViewStyles, originalFile)
  hideSelPopup()
  const { fraction, cfi, index, range, tocItem, section } = e.detail
  setLastCfi(cfi || lastCfi)
  // Индекс текущей главы foliate отдаёт как section.current: наружу view эмитит
  // не detail пагинатора, а собранный lastLocation ({...progress, tocItem,
  // pageItem, cfi, range}), и поля `index` в нём нет вовсе. Пока читали `index`,
  // lastIdx оставался null НАВСЕГДА — а на нём висят переход между главами
  // (gotoChapterStart) и края главы по Home/End (serg/tasks#996). `index`
  // оставлен запасным: если foliate когда-нибудь начнёт его слать, хуже не будет.
  const idx = typeof section?.current === 'number' ? section.current : index
  if (typeof idx === 'number') setLastIdx(idx)
  // Текстовый якорь верха экрана — основа устойчивого восстановления позиции.
  const anchor = captureAnchor(range)
  if (anchor) setLastAnchor(anchor)
  setLastFraction(fraction || 0)
  // Шкала (доля, «страница», текущая глава) — в progress-bar.js.
  updateProgress(e.detail)
  // Название текущей главы — в заголовке сверху (внизу больше не дублируется).
  setChapterTitle(tocItem?.label || '')
  markCurrentToc(tocItem)
  // Долистали до непереведённых абзацев — подтянуть их (дебаунс внутри).
  onTranslateRelocate()
  // Последняя страница книги — факт, а не порог доли: так и только так книга
  // становится «прочитанной» (serg/tasks#1853).
  const atEnd = isAtEnd(e.detail)
  // Дебаунс-сохранение прогресса на сервер (доля + CFI + текстовый якорь).
  // Потерь от дебаунса нет: уход вкладки/закрытие пишут место немедленно.
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    if (!currentWork) return
    // Далёкое от прошлого сохранения место = сервер положит прежнее в историю
    // переходов; кнопка «Назад» должна ожить сразу (serg/tasks#1078).
    noteProgressSaved(fraction || 0)
    savePosition(
      { ratio: fraction || 0, locator: cfi || '', text_anchor: anchor || '',
        chapter: tocItem?.label || '', at_end: atEnd })
  }, atEnd ? 0 : 900)
  if (ttsSt.advance) { ttsSt.advance = false; setTimeout(() => { if (ttsSt.active) ttsReadPage() }, 350) }
  // Дочитан до последней страницы — сбросить флаг обновления.
  if (atEnd && currentWork && libUpdated.has(currentWork.id)) {
    libUpdated.delete(currentWork.id)
    fetch(`/api/library/${currentWork.id}/update-flag`, { method: 'DELETE' })
      .catch((e) => logErr('clear update-flag', e))
  }
}

// Применение темы/шрифта/полей к содержимому книги.
function resolvedColor(varName) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim()
}
function bookCSS() {
  const fg = resolvedColor('--fg'), bg = resolvedColor('--bg'), accent = resolvedColor('--accent')
  const codeBg = resolvedColor('--bg-soft')
  const isDark = ['dusk', 'night', 'terminal', 'black', 'phosphor'].includes(prefs.theme)
  const colorScheme = isDark ? 'dark' : 'light'
  const fam = FONT_STACKS[prefs.fontFamily] || FONT_STACKS['pt-sans']
  // В режиме «лента» одна колонка должна занимать всю ширину экрана.
  // Поля задаём уровнем «Поля» (marginLevel → процент боковых отступов).
  const sidePad = MARGIN_SIDE_PCT[prefs.marginLevel] ?? MARGIN_SIDE_PCT[2]
  // Лента: распахиваем документ книги на всю ширину области (поля — паддингом body).
  // Корень узкой «ленты» был в shadow-гриде foliate (#top), пропатчен в paginator.js;
  // здесь — только распахивание самого документа и гашение возможных колонок.
  const scrolledBody = prefs.flow === 'scrolled'
    ? `html, body {
         max-width: none !important; width: auto !important; margin: 0 !important;
         column-width: auto !important; columns: auto !important;
       }
       body { padding: 0 ${sidePad}% !important; }
       img, svg, video, figure { max-width: 100% !important; }`
    : ''
  // Шрифт грузим только тот, которым читают. Пустая строка для системных
  // семейств: тогда в документе книги вообще нет обращения к сети за шрифтом,
  // и рендер не ждёт ответа стороннего хоста.
  const gf = gfontsFor(prefs.fontFamily)
  return `${gf ? `@import url('${gf}');` : ''}
    html, body { color-scheme: ${colorScheme}; background: ${bg} !important; color: ${fg} !important; }
    html { font-size: ${Math.round(prefs.fontScale * 100)}%; }
    body { font-family: ${fam}; }
    ${scrolledBody}
    /* Книги часто задают свой color на абзацах/спанах — он перебивает тему
       и в тёмных темах даёт нечитаемый серый. Форсируем наследование от body. */
    body *:not(a):not(mark) { color: inherit !important; }
    /* Фон примеров из EPUB должен соответствовать принудительному цвету текста.
       Вложенная подсветка тоже часто приносит белые подложки. */
    pre, code, kbd, samp, .highlight {
      background: ${codeBg} !important; color: ${fg} !important;
    }
    pre *, code *, kbd *, samp *, .highlight * {
      background: transparent !important; color: inherit !important;
    }
    a:link, a:visited { color: ${accent}; }
    html, body { text-align: left !important; }
    p, li, blockquote, dd { line-height: 1.55; text-align: left !important; }
    img, svg image { filter: ${isDark && prefs.imageMode !== 'original' ? 'brightness(.8) contrast(1.1)' : 'none'}; }
    img { max-width: 100%; height: auto; }
    .tts-reading { background: ${accent}28 !important; outline: 2px solid ${accent}88; outline-offset: 3px; border-radius: 3px; }
    ::highlight(tts-word) { background-color: ${accent}; color: #fff; border-radius: 2px; }
  `
}
export function applyViewStyles(target = view) {
  if (!target || !target.renderer) return
  const r = target.renderer
  if (r.setPDFOptions && target.book?.setPDFOptions) {
    r.setPDFOptions({ ...prefs, imageBookKey: currentWork?.id, rev: currentRev, cropURL: currentWork ? `/api/reader/${currentWork.id}/pdf-crop` : null, bg: resolvedColor('--bg'), fg: resolvedColor('--fg') }).catch(e => logErr('PDF: настройки', e))
  }
  // Сначала раскладка (flow/колонки), потом стили: render() триггерится атрибутами,
  // и к моменту его вызова наш bookCSS уже не перетирается лишним «paginated-кадром».
  if (prefs.flow === 'scrolled') {
    // Лента: одна колонка во всю ширину области (конкретный px, не «бесконечность»).
    // Ширина колонки = ширине области чтения. Раньше был floor 600 — на телефоне
    // (вьюпорт ~360) это давало колонку 600px → горизонтальный overflow и дёрганая
    // прокрутка. На десктопе clientWidth заведомо >600, поведение не меняется.
    const w = Math.max(280, ($('#view-host')?.clientWidth || 600))
    r.setAttribute('max-column-count', '1')
    r.setAttribute('max-inline-size', String(w))
  } else {
    // Страницы: 1 или 2 колонки по выбору; ширина колонки — от уровня полей,
    // НО не шире самой области чтения. MARGIN_INLINE (760/690/620/480) — десктопные
    // константы: на телефоне с вьюпортом ~360-540 колонка выходила шире экрана,
    // и правый край текста уезжал за границу — слова пропадали целиком, без
    // переноса и без горизонтального скролла. Для «Ленты» это уже чинили выше
    // (был floor 600), в «Страницах» дефект остался незамеченным, потому что на
    // широком мониторе его не видно.
    const cols = Math.max(1, Number(prefs.columns || 1))
    const host = Math.max(280, ($('#view-host')?.clientWidth || 600))
    const wanted = MARGIN_INLINE[prefs.marginLevel]
    r.setAttribute('max-column-count', String(cols))
    r.setAttribute('max-inline-size', String(Math.min(wanted, Math.floor(host / cols))))
  }
  const _sidePad = MARGIN_SIDE_PCT[prefs.marginLevel] ?? MARGIN_SIDE_PCT[2]
  // «Лента»: боковые поля даёт паддинг body -> gap 0 (иначе удваивается).
  // «Страницы»: gap = боковые поля.
  r.setAttribute('gap', (prefs.flow === 'scrolled' ? 0 : _sidePad) + '%')
  // Вертикальные поля страницы (foliate --_margin, дефолт 48px) — компактные, в ритме уровня полей.
  r.setAttribute('margin', String(MARGIN_GAP[prefs.marginLevel] ?? MARGIN_GAP[2]))
  r.setAttribute('flow', prefs.flow)
  r.setStyles?.(bookCSS())
}

// Открыть книгу сразу на выбранной главе (оглавление на странице книги,
// serg/tasks#1077). jump: { href, index, label } — координаты от сервера
// (backend/app/booktoc.py), href в формате самого foliate.
async function openAtChapter(view, jump) {
  const { href, index, label } = jump || {}
  try {
    await view.init({})
  } catch (e) {
    logErr('init перед переходом к главе не удался', e)
  }
  // href — то, чем адресует оглавление сам foliate (для FB2 это номер секции).
  if (href) {
    try {
      await view.goTo(href)
      return
    } catch (e) {
      logErr(`не открыл главу по href ${href}`, e)
    }
  }
  // Заголовок главы есть в тексте книги — ищем его тем же механизмом, что
  // восстанавливает позицию. Это переживает и расхождение нумерации секций.
  if (label) {
    const ok = await gotoPosition(view, { text_anchor: label, ratio: 0 })
    if (ok) return
  }
  if (typeof index === 'number' && index >= 0) {
    try {
      await view.goTo(index)
      return
    } catch (e) {
      logErr(`не открыл главу по индексу ${index}`, e)
    }
  }
  toast('Не нашёл эту главу в книге — открыл с начала', 'err')
}

// Подсветить в оглавлении главу, в которой стоит читатель. Панель открывается
// на ней (см. navigation.js), поэтому без отметки список бесполезен.
export function markCurrentToc(tocItem) {
  const list = $('#toc-list')
  if (!list) return
  const href = tocItem?.href || ''
  const label = (tocItem?.label || '').trim()
  let current = null
  for (const a of list.querySelectorAll('a')) {
    const hit = (href && a.dataset.href === href)
      || (!href && label && (a.querySelector('.toc-label')?.textContent || '').trim() === label)
    if (hit && !current) { a.setAttribute('aria-current', 'true'); current = a }
    else a.removeAttribute('aria-current')
  }
}

// Даты глав приходят отдельным запросом (их собирает сервер, см.
// app/chapterdates.py), поэтому дорисовываем их к уже построенному списку —
// ждать сеть перед показом оглавления незачем.
async function loadTocDates(workId) {
  let data = null
  try {
    data = await api.get(`/api/reader/${workId}/toc`)
  } catch (e) {
    logErr('даты глав не загрузились', e)
    return
  }
  if (!currentWork || currentWork.id !== workId) return
  const byHref = new Map()
  for (const it of data?.items || []) {
    if (it.date && it.href) byHref.set(it.href, it.date)
  }
  if (!byHref.size) return
  for (const a of $('#toc-list')?.querySelectorAll('a') || []) {
    if (a.querySelector('.toc-date')) continue
    const when = chapterDate(byHref.get(a.dataset.href))
    if (!when) continue
    const badge = document.createElement('span')
    badge.className = 'toc-date'
    badge.textContent = when
    a.append(badge)
  }
}

function buildTOC() {
  const toc = view?.book?.toc || []
  const list = $('#toc-list'); list.innerHTML = ''
  let _firstToc = true
  const add = (items, sub) => {
    for (const it of items) {
      const a = document.createElement('a')
      const text = document.createElement('span')
      text.className = 'toc-label'
      text.textContent = it.label || '—'
      a.append(text)
      if (sub) a.className = 'toc-sub'
      a.href = '#'
      a.dataset.href = it.href || ''
      const _isFirst = _firstToc && !sub
      if (!sub) _firstToc = false
      a.addEventListener('click', (ev) => {
        ev.preventDefault()
        // Уход по оглавлению — прыжок: откуда ушли, туда должно возвращать
        // «Назад» в тулбаре (serg/tasks#1078).
        recordJump('toc')
        if (_isFirst) view.goToFraction(0); else view.goTo(it.href)
        closePanels()
      })
      list.append(a)
      if (it.subitems?.length) add(it.subitems, true)
    }
  }
  add(toc, false)
}

// Вкладку свернули/закрыли, PWA ушло в фон: записываем место СЕЙЧАС, keepalive —
// запрос дойдёт и после выгрузки страницы. Без этого последние секунды чтения
// (окно дебаунса) терялись, а убитое в фоне приложение открывалось не там.
function onLeave() {
  if (!currentWork || $('#reader')?.hidden) return
  flushPosition({ keepalive: true })
}
window.addEventListener('pagehide', onLeave)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') onLeave()
})
// Досылка позиций, не ушедших в прошлый раз (сеть, убитая вкладка).
replayPending()
