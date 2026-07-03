// Ядро читалки: открытие книги, релокейт/прогресс, стили книги, TOC.
import { $ } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { prefs, MARGIN_INLINE, FONT_STACKS, GFONTS } from './core/prefs.js'
import { view, currentWork, lastCfi, libMonitored, libUpdated, navStack,
         setView, setCurrentWork, setLastCfi, setLastIdx } from './core/state.js'
import { ttsSt, ttsStop, ttsReadPage } from './tts.js'
import { loadHighlightsWeb, onDrawAnnotation, hideSelPopup } from './highlights.js'
import { attachKeysToDoc, closePanels } from './navigation.js'

// ===================== ЧИТАЛКА =====================
let saveTimer = null

// Восстановление позиции чтения при открытии книги:
//  • есть точный локатор (CFI) → инициализируем прямо на нём;
//  • иначе стартуем с начала текста и, если известна доля прочитанного
//    (ratio, напр. импорт из ReadEra), доезжаем до неё;
//  • иначе — просто начало книги.
async function restoreReadingPosition(view, prog) {
  if (prog && prog.locator) {
    await view.init({ lastLocation: prog.locator })
    return
  }
  await view.init({ showTextStart: true })
  if (prog && prog.ratio > 0) {
    try { await view.goToFraction(prog.ratio) } catch {}
  }
}

export async function openReader(work) {
  ttsStop()
  setCurrentWork(work)
  document.body.classList.add('reader-open')
  $('#library').hidden = true
  $('#reader').hidden = false
  $('#reader-title').textContent = work.title || ''
  const updBtn = $('#update-btn')
  updBtn.hidden = !libMonitored.has(work.id)
  updBtn.dataset.state = ''
  updBtn.title = 'Проверить новые главы'

  // История: открытие книги — отдельная запись, чтобы браузерный «назад»
  // возвращал в библиотеку, а не уводил с сайта.
  if (!(history.state && history.state.reader)) {
    history.pushState({ reader: true }, '', '#read')
  }

  // Очистить прошлый экземпляр.
  $('#view-host').innerHTML = ''
  setView(document.createElement('foliate-view'))
  $('#view-host').append(view)

  // Загружаем файл как Blob → File с корректным именем (для детекта FB2).
  const resp = await fetch(`/api/reader/${work.id}/file`)
  const blob = await resp.blob()
  const name = `book.${work.file_format || 'epub'}`
  const file = new File([blob], name, { type: blob.type })

  await view.open(file)
  navStack.length = 0
  document.getElementById('reader')?.classList.remove('chrome-hidden')
  view.addEventListener('relocate', onRelocate)
  view.addEventListener('load', attachKeysToDoc)
  view.addEventListener('draw-annotation', onDrawAnnotation)
  applyViewStyles()
  buildTOC()

  // Восстановить позицию: точный CFI, иначе ratio (напр. импорт из ReadEra), иначе начало.
  const prog = await api.get(`/api/progress/${work.id}`).catch(() => null)
  await restoreReadingPosition(view, prog)

  // Подтянуть и нарисовать сохранённые подсветки (синк с сервером/Android).
  loadHighlightsWeb()
}

function onRelocate(e) {
  hideSelPopup()
  const { fraction, cfi, index } = e.detail
  setLastCfi(cfi || lastCfi)
  if (typeof index === 'number') setLastIdx(index)
  const pct = Math.round((fraction || 0) * 100)
  $('#progress-slider').value = fraction || 0
  $('#progress-label').textContent = pct + '%'
  // Дебаунс-сохранение прогресса на сервер.
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    if (!currentWork) return
    api.put(`/api/progress/${currentWork.id}`, { ratio: fraction || 0, locator: cfi || '' })
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
  const fam = FONT_STACKS[prefs.fontFamily] || FONT_STACKS['merriweather']
  // В режиме «лента» одна колонка должна занимать всю ширину экрана.
  // Поля задаём уровнем «Поля» (marginLevel → процент боковых отступов).
  const sidePad = { 0: 0, 1: 5, 2: 12 }[prefs.marginLevel] ?? 5
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
  return `@import url('${GFONTS}');
    html, body { color-scheme: ${colorScheme}; background: ${bg} !important; color: ${fg} !important; }
    html { font-size: ${Math.round(prefs.fontScale * 100)}%; }
    body { font-family: ${fam}; }
    ${scrolledBody}
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
    // Страницы: 1 или 2 колонки по выбору; ширина колонки — от уровня полей.
    r.setAttribute('max-column-count', String(prefs.columns || 1))
    r.setAttribute('max-inline-size', String(MARGIN_INLINE[prefs.marginLevel]))
  }
  const _sidePad = { 0: 0, 1: 5, 2: 12 }[prefs.marginLevel] ?? 5
  // «Лента»: боковые поля даёт паддинг body -> gap 0 (иначе удваивается).
  // «Страницы»: gap = боковые поля.
  r.setAttribute('gap', (prefs.flow === 'scrolled' ? 0 : _sidePad) + '%')
  // Вертикальные поля страницы (foliate --_margin, дефолт 48px) — компактные, в ритме уровня полей.
  r.setAttribute('margin', String({ 0: 0, 1: 16, 2: 34 }[prefs.marginLevel] ?? 16))
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
