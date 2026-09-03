// Компактный «хром» читалки: заголовок-переключатель, меню «Ещё», индикаторы
// состояния на кнопке ⋮ и полоса сетевой работы под верхней панелью.
//
// Зачем отдельный модуль: раньше в верхней панели стояли восемь кнопок, а
// название главы дублировалось строкой внизу — на телефоне в ландшафте тексту
// оставалось 64% экрана. Снаружи оставлены только частые действия
// (‹ ☰ 🔍 Aa ⛶ ⋮), редкие ушли в панель «Ещё», а их состояние видно точкой на
// самой кнопке ⋮ — иначе перенос в меню сделал бы фоновую проверку глав
// невидимой.
import { $ } from './core/dom.js'

// --- Заголовок: глава → книга → автор → снова глава -------------------------
// Дефолт при открытии книги — глава: это мгновенная ориентация в тексте, ради
// неё заголовок и переехал наверх. Выбор пользователя липкий внутри книги
// (relocate его не сбрасывает — это ощущалось бы багом), но не переживает
// открытие следующей книги.
const MODES = ['chapter', 'book', 'author']
let meta = { chapter: '', book: '', author: '' }
let mode = 'chapter'

// Первый непустой режим, начиная с желаемого: у книги может не быть автора, у
// позиции — главы (обложка, титул), и заголовок не должен становиться пустым.
function resolveMode(want) {
  const from = Math.max(0, MODES.indexOf(want))
  for (let i = 0; i < MODES.length; i++) {
    const m = MODES[(from + i) % MODES.length]
    if (meta[m]) return m
  }
  return want
}

function renderTitle() {
  const el = $('#reader-title')
  if (!el) return
  const m = resolveMode(mode)
  el.dataset.mode = m
  el.textContent = meta[m] || ''
  const names = { chapter: 'Глава', book: 'Книга', author: 'Автор' }
  el.title = `${names[m]}: ${meta[m] || '—'} — нажмите, чтобы показать другое`
}

// Вызывается при открытии книги: сбрасывает цикл на «главу».
export function setBookMeta(work) {
  meta = { chapter: '', book: (work?.title || '').trim(), author: (work?.author || '').trim() }
  mode = 'chapter'
  renderTitle()
}

// Вызывается на каждый relocate. Режим НЕ трогаем.
export function setChapterTitle(label) {
  meta.chapter = (label || '').trim()
  renderTitle()
}

// --- Индикаторы на кнопке ⋮ -------------------------------------------------
// Действие лежит в закрытом меню, но его состояние должно быть видно снаружи:
// 'new' — найдены новые главы, 'err' — проверка не удалась, '' — чисто.
export function setMoreBadge(state) {
  const b = $('#more-btn')
  if (!b) return
  if (state) b.dataset.badge = state
  else delete b.dataset.badge
}

// Включённый режим (перевод) подсвечивает саму кнопку ⋮ — вторая постоянная
// иконка снаружи стёрла бы весь выигрыш редизайна.
export function setMoreActive(name) {
  const b = $('#more-btn')
  if (!b) return
  if (name) b.dataset.active = name
  else delete b.dataset.active
}

// Тонкая полоса под верхней панелью: сетевая работа в поле зрения читающего.
export function showTopLoading(on) {
  const el = $('#top-loading')
  if (el) el.hidden = !on
}

// --- Двойной тап по центру → полный экран ------------------------------------
// Тайминги берём из progress-bar.js: один «бюджет жестов» на всё приложение.
import { HOLD_MS, MOVE_TOL } from './progress-bar.js'

export function toggleFullscreen() {
  const d = document
  if (d.fullscreenElement || d.webkitFullscreenElement) {
    (d.exitFullscreen || d.webkitExitFullscreen)?.call(d)
  } else {
    const el = d.documentElement
    ;(el.requestFullscreen || el.webkitRequestFullscreen)?.call(el)
  }
}

// Двойной тап, а не долгое нажатие: долгое нажатие — основной путь к выделению
// текста, на котором держится highlights.js. Но и двойной тап в Chrome выделяет
// слово, поэтому намерение различаем по getSelection() ПОСЛЕ жеста: пусто —
// наш жест, есть выделение — отдаём его попапу «выделить/перевести/копировать».
//
// Проверка синхронная: requestFullscreen() требует жеста пользователя и из
// setTimeout уже не сработает.
const CENTER_FROM = 0.25, CENTER_TO = 0.75  // центральная зона по ширине
let tapT = 0, tapX = 0, tapY = 0

function selectionEmpty(win) {
  try {
    const sel = win?.getSelection?.()
    if (!sel) return true
    return sel.isCollapsed || !String(sel).trim()
  } catch { return true }
}

// Один обработчик и для документа книги (тап по тексту), и для #view-host (тап
// по пустому месту вокруг колонки — там события до iframe не доходят).
function onTapEnd(clientX, clientY, startX, startY, startedAt, width, win) {
  const moved = Math.abs(clientX - startX) > MOVE_TOL || Math.abs(clientY - startY) > MOVE_TOL
  const quick = Date.now() - startedAt < 500
  if (moved || !quick) { tapT = 0; return false }
  const rel = width > 0 ? clientX / width : 0.5
  if (rel < CENTER_FROM || rel > CENTER_TO) { tapT = 0; return false }

  const now = Date.now()
  const isSecond = tapT && (now - tapT) < HOLD_MS &&
    Math.abs(clientX - tapX) < MOVE_TOL * 3 && Math.abs(clientY - tapY) < MOVE_TOL * 3
  if (isSecond) {
    tapT = 0
    if (!selectionEmpty(win) || !selectionEmpty(window)) return false
    toggleFullscreen()
    return true
  }
  tapT = now; tapX = clientX; tapY = clientY
  return false
}

// Подключается из navigation.js на документ книги при каждой загрузке главы.
export function attachDoubleTapFullscreen(doc) {
  if (!doc || doc.__dblTapFs) return
  doc.__dblTapFs = true
  let sx = 0, sy = 0, st = 0
  doc.addEventListener('touchstart', (ev) => {
    const t = ev.changedTouches[0]
    sx = t.clientX; sy = t.clientY; st = Date.now()
  }, { passive: true })
  doc.addEventListener('touchend', (ev) => {
    const t = ev.changedTouches[0]
    const w = doc.documentElement?.clientWidth || doc.defaultView?.innerWidth || 0
    onTapEnd(t.clientX, t.clientY, sx, sy, st, w, doc.defaultView)
  }, { passive: true })
}

export function initChrome() {
  $('#reader-title')?.addEventListener('click', () => {
    const cur = resolveMode(mode)
    // Крутим от ПОКАЗАННОГО режима, а не от желаемого: иначе на книге без
    // автора тап по «книге» возвращал бы «книгу» же.
    mode = MODES[(MODES.indexOf(cur) + 1) % MODES.length]
    renderTitle()
  })

  // Пустое место вокруг колонки текста: события туда не доходят до iframe.
  const host = $('#view-host')
  if (host) {
    let sx = 0, sy = 0, st = 0
    host.addEventListener('touchstart', (ev) => {
      const t = ev.changedTouches[0]
      sx = t.clientX; sy = t.clientY; st = Date.now()
    }, { passive: true })
    host.addEventListener('touchend', (ev) => {
      // Края отданы зонам перелистывания — там свой обработчик.
      if (ev.target?.classList?.contains('tap-zone')) return
      const t = ev.changedTouches[0]
      onTapEnd(t.clientX, t.clientY, sx, sy, st, host.clientWidth, window)
    }, { passive: true })
  }
}
