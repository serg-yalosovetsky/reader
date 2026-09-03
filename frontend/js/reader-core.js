// Ядро читалки: открытие книги, релокейт/прогресс, стили книги, TOC.
import { $, toast } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { prefs, MARGIN_INLINE, MARGIN_SIDE_PCT, MARGIN_GAP, FONT_STACKS,
         gfontsFor } from './core/prefs.js'
import { view, currentWork, lastCfi, libMonitored, libUpdated, navStack,
         setView, setCurrentWork, setLastCfi, setLastAnchor, setLastIdx } from './core/state.js'
import { restorePosition, captureAnchor } from './core/position.js'
import { inlineImagesOnLoad } from './core/inline-images.js'
import { ttsSt, ttsStop, ttsReadPage } from './tts.js'
import { loadHighlightsWeb, onDrawAnnotation, hideSelPopup } from './highlights.js'
import { attachKeysToDoc, closePanels } from './navigation.js'
import { cachedBook } from './core/offline.js'
import { updateProgress, buildChapterMarks } from './progress-bar.js'
import { setBookMeta, setChapterTitle, setMoreBadge } from './chrome.js'
import { onTranslateRelocate, resetTranslate } from './translate.js'
import { convertible, pdfAsEpub, ensureEpub } from './core/convert.js'

// ===================== ЧИТАЛКА =====================
let saveTimer = null

// opts.original=true — открыть исходный файл (PDF как макет), минуя EPUB-версию.
export async function openReader(work, opts = {}) {
  ttsStop()
  setCurrentWork(work)
  document.body.classList.add('reader-open')
  $('#library').hidden = true
  $('#reader').hidden = false
  // Заголовок сверху: по умолчанию — название главы (ставится первым
  // relocate), по тапу переключается на книгу и автора.
  setBookMeta(work)
  resetTranslate()
  const updBtn = $('#update-btn')
  updBtn.hidden = !libMonitored.has(work.id)
  updBtn.dataset.state = ''
  updBtn.disabled = false
  updBtn.title = 'Проверить новые главы'
  { const _st = $('#update-state'); if (_st) _st.textContent = '' }
  setMoreBadge('')

  // История/URL: URL отражает ОТКРЫТУЮ книгу (#read/<id>), чтобы при
  // переоткрытии вкладки читалка вернулась к той же книге (позицию хранит
  // сервер). Вход из библиотеки/страницы книги — новая запись (браузерный
  // «назад» → назад по сайту); переключение книги внутри читалки или
  // восстановление из URL — замена записи, чтобы не плодить историю.
  const bookHash = `#read/${encodeURIComponent(work.id)}`
  const histState = { reader: true, id: work.id }
  if ((history.state && history.state.reader) || location.hash === bookHash) {
    history.replaceState(histState, '', bookHash)
  } else {
    history.pushState(histState, '', bookHash)
  }

  // Очистить прошлый экземпляр.
  $('#view-host').innerHTML = ''
  setView(document.createElement('foliate-view'))
  $('#view-host').append(view)

  // Загружаем файл как Blob → File с корректным именем (для детекта FB2).
  // Cache-first: если книга сохранена офлайн — читаем из кэша (без сети),
  // иначе тянем с сервера. Помогает на телефоне при плохом коннекте.
  // Прогресс не зависит от файла книги, а раньше запрашивался ПОСЛЕ его
  // открытия — лишний круг по сети посреди пути к первому кадру. Запускаем
  // сразу, ждём ниже, там где он реально нужен.
  const progP = api.get(`/api/progress/${work.id}`).catch(() => null)
  let resp = await cachedBook(work.id)
  // PDF читаем как EPUB (перетекающий текст). Первое открытие ждёт конвертацию
  // (секунды–минуты на толстой книге), дальше файл готов и отдаётся сразу.
  if (!resp && !opts.original && convertible(work) && pdfAsEpub()) {
    const t = toast('Перевожу в EPUB — будет перетекающий текст…', 'info', 60000)
    const st = await ensureEpub(work.id).catch(() => null)
    t?.close?.()
    if (!st?.ready) {
      toast(st?.error ? 'Не вышло сделать EPUB: ' + st.error : 'Конвертация ещё идёт — открываю оригинал', 'err', 6000)
    }
  }
  if (!resp) resp = await fetch(`/api/reader/${work.id}/file` + (opts.original ? '?original=1' : ''))
  const blob = await resp.blob()
  // Формат берём из ответа: сервер мог отдать EPUB вместо PDF (имя файла решает,
  // каким движком foliate будет рендерить).
  const fmt = resp.headers?.get?.('X-Book-Format') || work.file_format || 'epub'
  const name = `book.${fmt}`
  const file = new File([blob], name, { type: blob.type })

  await view.open(file)
  navStack.length = 0
  document.getElementById('reader')?.classList.remove('chrome-hidden')
  view.addEventListener('relocate', onRelocate)
  view.addEventListener('load', attachKeysToDoc)
  view.addEventListener('load', inlineImagesOnLoad)
  view.addEventListener('draw-annotation', onDrawAnnotation)
  applyViewStyles()
  buildTOC()
  buildChapterMarks()

  // Восстановить позицию: текстовый якорь (устойчив к пересборке книги), иначе
  // CFI, иначе доля (напр. импорт из ReadEra), иначе начало. См. core/position.js.
  const prog = await progP
  await restorePosition(view, prog)

  // Подтянуть и нарисовать сохранённые подсветки (синк с сервером/Android).
  loadHighlightsWeb()
}

function onRelocate(e) {
  hideSelPopup()
  const { fraction, cfi, index, range, tocItem } = e.detail
  setLastCfi(cfi || lastCfi)
  if (typeof index === 'number') setLastIdx(index)
  // Текстовый якорь верха экрана — основа устойчивого восстановления позиции.
  const anchor = captureAnchor(range)
  if (anchor) setLastAnchor(anchor)
  // Шкала (доля, «страница», текущая глава) — в progress-bar.js.
  updateProgress(e.detail)
  // Название текущей главы — в заголовке сверху (внизу больше не дублируется).
  setChapterTitle(tocItem?.label || '')
  // Долистали до непереведённых абзацев — подтянуть их (дебаунс внутри).
  onTranslateRelocate()
  // Дебаунс-сохранение прогресса на сервер (доля + CFI + текстовый якорь).
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    if (!currentWork) return
    api.put(`/api/progress/${currentWork.id}`,
      { ratio: fraction || 0, locator: cfi || '', text_anchor: anchor || '' })
      .catch((e) => logErr('save progress', e))
  }, 900)
  if (ttsSt.advance) { ttsSt.advance = false; setTimeout(() => { if (ttsSt.active) ttsReadPage() }, 350) }
  // Дочитан до конца — сбросить флаг обновления
  if (fraction >= 0.98 && currentWork && libUpdated.has(currentWork.id)) {
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
  const isDark = ['dusk', 'night', 'terminal', 'black'].includes(prefs.theme)
  const colorScheme = isDark ? 'dark' : 'light'
  const fam = FONT_STACKS[prefs.fontFamily] || FONT_STACKS['inter']
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
    a:link, a:visited { color: ${accent}; }
    html, body { text-align: left !important; }
    p, li, blockquote, dd { line-height: 1.55; text-align: left !important; }
    img { max-width: 100%; height: auto; }
    .tts-reading { background: ${accent}28 !important; outline: 2px solid ${accent}88; outline-offset: 3px; border-radius: 3px; }
    ::highlight(tts-word) { background-color: ${accent}; color: #fff; border-radius: 2px; }
  `
}
export function applyViewStyles() {
  if (!view || !view.renderer) return
  const r = view.renderer
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

function buildTOC() {
  const toc = view?.book?.toc || []
  const list = $('#toc-list'); list.innerHTML = ''
  let _firstToc = true
  const add = (items, sub) => {
    for (const it of items) {
      const a = document.createElement('a')
      a.textContent = it.label || '—'
      if (sub) a.className = 'toc-sub'
      a.href = '#'
      const _isFirst = _firstToc && !sub
      if (!sub) _firstToc = false
      a.addEventListener('click', (ev) => {
        ev.preventDefault()
        if (_isFirst) view.goToFraction(0); else view.goTo(it.href)
        closePanels()
      })
      list.append(a)
      if (it.subitems?.length) add(it.subitems, true)
    }
  }
  add(toc, false)
}
