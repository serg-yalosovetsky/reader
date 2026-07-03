// Подсветки/цитаты (синк с сервером + Android) + попап выделения.
import { $, escapeHtml } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { isWebCfi, goToLocator } from './core/locator.js'
import { currentWork, view, _selIndex } from './core/state.js'
import { Overlayer } from '/vendor/foliate-js/overlayer.js'
import { openPanel, closePanels } from './navigation.js'

// ===================== Подсветки / цитаты (синк с сервером + Android) =====================
// Сервер хранит locator как строку. Веб пишет foliate-CFI и рисует оверлей; Android
// пишет Readium-JSON. Визуальный оверлей рисуется только на своей платформе, но список
// цитат и переход (locator→fraction-фолбэк) общие. Цвет хранится именем.
const HL_DRAW = { yellow: '#f2d544', green: '#79c267', blue: '#5aa9f0', pink: '#ef7db5' }
let _highlights = []
let _pendingSel = null

export function onDrawAnnotation(e) {
  const { draw, annotation } = e.detail
  draw(Overlayer.highlight, { color: HL_DRAW[annotation.color] || annotation.color || HL_DRAW.yellow })
}

export function hideSelPopup() { $('#sel-popup').hidden = true; _pendingSel = null }

export function onSelectionChanged(doc) {
  const sel = doc.getSelection?.()
  const text = sel?.toString().trim() || ''
  if (!sel || sel.isCollapsed || !text) { hideSelPopup(); return }
  const range = sel.getRangeAt(0)
  _pendingSel = { range, index: _selIndex, text }
  // Координаты выделения в iframe → в окно верхнего документа (попап position:fixed).
  const rect = range.getBoundingClientRect()
  const fr = doc.defaultView?.frameElement?.getBoundingClientRect() || { left: 0, top: 0 }
  const popup = $('#sel-popup')
  popup.hidden = false
  const x = Math.max(8, Math.min(fr.left + rect.left, window.innerWidth - popup.offsetWidth - 8))
  const y = Math.max(8, fr.top + rect.top - popup.offsetHeight - 8)
  popup.style.left = x + 'px'
  popup.style.top = y + 'px'
}

async function doHighlightSelection() {
  const sel = _pendingSel
  if (!sel || !currentWork || !view) { hideSelPopup(); return }
  let cfi = ''
  try { cfi = view.getCFI(sel.index, sel.range) } catch {}
  hideSelPopup()
  if (cfi) { try { await view.addAnnotation({ value: cfi, color: 'yellow' }) } catch {} }
  const ratio = parseFloat($('#progress-slider').value) || 0
  try {
    await api.post(`/api/highlights/${currentWork.id}`, { ratio, locator: cfi, text: sel.text, color: 'yellow' })
    loadHighlightsWeb()
  } catch {}
}

function doTranslateSelection() {
  const text = _pendingSel?.text
  hideSelPopup()
  if (!text) return
  const url = 'https://translate.google.com/?sl=auto&tl=ru&op=translate&text=' + encodeURIComponent(text)
  window.open(url, '_blank', 'noopener')
}

function doCopySelection() {
  const text = _pendingSel?.text
  hideSelPopup()
  if (text) navigator.clipboard?.writeText(text).catch(() => {})
}

export async function loadHighlightsWeb() {
  if (!currentWork) return
  let items = []
  try { items = await api.get(`/api/highlights/${currentWork.id}`) } catch { items = [] }
  _highlights = items
  // Нарисовать свои (foliate-CFI) подсветки; чужие (Android Readium-JSON) пропустить.
  for (const hl of items) {
    if (isWebCfi(hl.locator)) {
      try { await view.addAnnotation({ value: hl.locator, color: hl.color || 'yellow' }) } catch {}
    }
  }
  renderHighlights()
}

function renderHighlights() {
  const list = $('#hl-list'); list.innerHTML = ''
  if (!_highlights.length) {
    const p = document.createElement('p'); p.className = 'panel-empty'
    p.textContent = 'Выделите текст в книге → «Выделить»'
    list.append(p); return
  }
  for (const hl of _highlights) {
    const row = document.createElement('div'); row.className = 'bm-row'
    const a = document.createElement('a'); a.href = '#'; a.className = 'bm-link'
    const pct = Math.round((hl.ratio || 0) * 100)
    a.textContent = `${(hl.text || 'Выделение').slice(0, 80)} · ${pct}%`
    a.addEventListener('click', async (ev) => {
      ev.preventDefault()
      await goToLocator(view, hl.locator, hl.ratio)
      closePanels()
    })
    const del = document.createElement('button')
    del.className = 'icon-btn bm-del'; del.textContent = '✕'; del.title = 'Удалить'
    del.addEventListener('click', async (ev) => {
      ev.stopPropagation(); ev.preventDefault()
      try { await fetch(`/api/highlights/id/${hl.id}`, { method: 'DELETE' }) } catch (e) { logErr('delete highlight', e) }
      if (isWebCfi(hl.locator)) { try { await view.deleteAnnotation({ value: hl.locator }) } catch {} }
      loadHighlightsWeb()
    })
    row.append(a, del); list.append(row)
  }
}

$('#hl-btn').addEventListener('click', () => { openPanel('#hl-panel'); loadHighlightsWeb() })
$('#sel-highlight').addEventListener('click', doHighlightSelection)
$('#sel-translate').addEventListener('click', doTranslateSelection)
$('#sel-copy').addEventListener('click', doCopySelection)
