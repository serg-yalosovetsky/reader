// Автоскрытие не трогает размеры foliate: панели в полном экране — оверлеи.
import { prefs } from './core/prefs.js'
import { isFullscreen } from './core/fullscreen.js'
import { MENU_HIDE_MS, menuAutohideEnabled, shouldRevealMenu } from './core/menu-autohide.js'
let format = '', timer, reader, bars = [], active = false
let hovering = false, keyboardFocus = false, interacting = false
const inBar = el => bars.some(bar => bar.contains(el))
const panelOpen = () => !!reader?.querySelector('.panel:not([hidden])')
function visible(show) {
  if (!show && inBar(document.activeElement)) document.activeElement.blur()
  reader.classList.toggle('menu-hidden', !show)
  for (const bar of bars) {
    bar.inert = !show
    if (show) bar.removeAttribute('aria-hidden')
    else bar.setAttribute('aria-hidden', 'true')
  }
}
function schedule() {
  clearTimeout(timer)
  if (!active || hovering || interacting || (keyboardFocus && inBar(document.activeElement)) || panelOpen()) return
  timer = setTimeout(() => visible(false), MENU_HIDE_MS)
}
export function revealReaderMenu() {
  if (!active) return
  visible(true); schedule()
}
export function refreshMenuAutohide() {
  if (!reader) return
  active = !reader.hidden && isFullscreen() && menuAutohideEnabled(format, prefs)
  // Оверлей только при включённом автоскрытии: без него панели в раскладке,
  // иначе в EPUB (по умолчанию выкл.) они закрывали бы верхнюю и нижнюю строки.
  reader.classList.toggle('fullscreen-menu', active)
  visible(true); schedule()
}
export function setMenuFormat(value) { format = value; refreshMenuAutohide() }
// События iframe не всплывают наружу, поэтому подключаем каждый документ главы.
export function attachMenuDocument(doc) {
  if (doc.__menuAutohide) return
  doc.__menuAutohide = true
  doc.addEventListener('keydown', e => {
    if (!active) return
    if (shouldRevealMenu(e)) {
      if (e.key === 'Tab') keyboardFocus = true
      revealReaderMenu()
      // Tab из iframe переводит фокус в возвращённую панель.
      if (e.key === 'Tab' && doc !== document) {
        e.preventDefault()
        bars[e.shiftKey ? 1 : 0].querySelector('button')?.focus()
      }
    }
  }, true)
  doc.addEventListener('pointermove', e => {
    // Координаты iframe переводим в координаты экрана читалки.
    const offset = doc === document ? 0 : (doc.defaultView?.frameElement?.getBoundingClientRect().top || 0)
    if (shouldRevealMenu({ type: e.type, mouse: e.pointerType === 'mouse',
      y: e.clientY + offset, height: window.innerHeight, panel: inBar(e.target) })) revealReaderMenu()
  }, { passive: true })
  let start
  doc.addEventListener('pointerdown', e => { start = { x: e.clientX, y: e.clientY } }, { passive: true })
  doc.addEventListener('click', e => {
    if (doc === document && !e.target.closest('#view-host')) return
    if (e.target.closest?.('a, button, input, .tap-zone')) return
    if (!start || Math.abs(start.x - e.clientX) > 12 || Math.abs(start.y - e.clientY) > 12) return
    if (!doc.getSelection()?.isCollapsed) return
    const width = doc === document ? window.innerWidth : doc.documentElement.clientWidth
    if (shouldRevealMenu({ type: 'click', center: e.clientX >= width * .25 && e.clientX <= width * .75 })) revealReaderMenu()
  })
}
export function initMenuAutohide() {
  reader = document.querySelector('#reader')
  bars = [...reader.querySelectorAll('#reader-top, #reader-bottom')]
  for (const bar of bars) {
    bar.addEventListener('pointerenter', e => {
      if (e.pointerType !== 'mouse') return
      hovering = true; revealReaderMenu()
    })
    bar.addEventListener('pointerleave', () => { hovering = false; schedule() })
  }
  document.addEventListener('keydown', e => {
    if (e.key === 'Tab') keyboardFocus = true
  }, true)
  document.addEventListener('pointerdown', e => {
    const wasPinned = keyboardFocus && inBar(document.activeElement)
    keyboardFocus = false
    // Кнопки листания не продлевают таймер, остальные действия в меню — да.
    if (inBar(e.target) && !e.target.closest('#prev-btn, #next-btn')) {
      interacting = true; revealReaderMenu()
    } else if (wasPinned) schedule()
  }, true)
  for (const event of ['pointerup', 'pointercancel']) document.addEventListener(event, () => {
    if (interacting) { interacting = false; schedule() }
  }, true)
  reader.addEventListener('focusin', e => {
    if (shouldRevealMenu({ type: e.type, panel: inBar(e.target) })) revealReaderMenu()
  })
  reader.addEventListener('focusout', () => queueMicrotask(() => {
    if (!inBar(document.activeElement)) { keyboardFocus = false; schedule() }
  }))
  // Индикатор загрузки тоже меняет hidden при листании; наблюдаем только меню.
  const panelsObserver = new MutationObserver(() => {
    if (panelOpen()) revealReaderMenu()
    else schedule()
  })
  for (const panel of reader.querySelectorAll('.panel')) {
    panelsObserver.observe(panel, { attributes: true, attributeFilter: ['hidden'] })
  }
  new MutationObserver(refreshMenuAutohide).observe(reader, { attributes: true, attributeFilter: ['hidden'] })
  for (const event of ['fullscreenchange', 'webkitfullscreenchange']) document.addEventListener(event, refreshMenuAutohide)
  attachMenuDocument(document)
}
