// Шкала прогресса читалки: перемотка ТОЛЬКО по зажатию, метки глав, подсказка.
//
// Зачем зажатие. Нижняя панель прижата к краю экрана — ровно туда, откуда на
// Android начинается системный жест «снизу вверх» (последние приложения).
// Нативный <input type=range> отзывался на первое же касание: палец уходил
// вверх, а книга успевала перемотаться на случайную долю. Теперь короткий тап
// и свайп по шкале не делают ничего; трек «просыпается» только после HOLD_MS
// удержания — и тогда показывает проценты, страницу и главу под пальцем.
//
// Сам <input id="progress-slider"> остаётся в разметке скрытым: он хранит
// текущую долю для закладок и подсветок ($('#progress-slider').value).
import { $ } from './core/dom.js'
import { view } from './core/state.js'

const HOLD_MS = 320   // сколько держать палец, чтобы шкала проснулась
const MOVE_TOL = 12   // px: сдвиг раньше этого — это свайп, а не зажатие

let marks = []        // [{ fraction, label }] — начала глав из оглавления
let lastLoc = null    // location из relocate: { current, next, total }
let curFrac = 0       // доля, показанная последним relocate
let dragFrac = 0      // доля под пальцем во время перетаскивания
let armed = false     // шкала проснулась, палец ведёт ползунок
let holdTimer = null, startX = 0, startY = 0, activeId = null

const clamp01 = (v) => Math.min(1, Math.max(0, v))
const pct = (f) => Math.round(f * 100) + '%'

// ===================== отрисовка =====================
function paint(f) {
  const fill = $('#progress-fill'), thumb = $('#progress-thumb'), wrap = $('#progress-wrap')
  if (!fill || !thumb) return
  const p = clamp01(f) * 100
  fill.style.width = p + '%'
  thumb.style.left = p + '%'
  wrap?.setAttribute('aria-valuenow', String(Math.round(p)))
  wrap?.setAttribute('aria-valuetext', bubbleText(f).replace('\n', ', '))
}

// Глава, в которую попадает доля f (последняя метка не правее f).
function chapterAt(f) {
  let label = ''
  for (const m of marks) { if (m.fraction <= f + 1e-6) label = m.label; else break }
  return label
}

// «стр. 128 / 305». Страницы у EPUB условные — foliate считает их как
// «локации» по ~1500 символов; номера устойчивы при смене шрифта и полей.
function pageAt(f) {
  const total = lastLoc?.total
  if (!total) return ''
  const cur = Math.min(total, Math.max(1, Math.round(f * total) || 1))
  return `стр. ${cur} / ${total}`
}

function bubbleText(f) {
  const head = [pct(f), pageAt(f)].filter(Boolean).join('  ·  ')
  const ch = chapterAt(f)
  return ch ? head + '\n' + ch : head
}

function showBubble(f) {
  const b = $('#progress-bubble'), track = $('#progress-track')
  if (!b || !track) return
  b.textContent = bubbleText(f)
  b.hidden = false
  // Координаты окна: подсказка position:fixed (панель обрезает всё, что
  // вылезает за её верх). Держим её над пальцем, но внутри экрана.
  const r = track.getBoundingClientRect()
  const half = b.offsetWidth / 2
  const x = r.left + clamp01(f) * r.width
  b.style.left = Math.min(window.innerWidth - half - 6, Math.max(half + 6, x)) + 'px'
  // Вертикально тоже держим в экране: в альбомной ориентации панель близко к
  // верху, и трёхстрочная подсказка иначе уезжает за кромку.
  const bottom = window.innerHeight - r.top + 10
  b.style.bottom = Math.round(Math.min(bottom, window.innerHeight - b.offsetHeight - 6)) + 'px'
}
function hideBubble() { const b = $('#progress-bubble'); if (b) b.hidden = true }

// ===================== публичное API =====================
// Новая позиция из foliate (relocate). Пока палец ведёт ползунок, не
// перебиваем его: пользователь смотрит на свою цель, а не на текущую точку.
export function updateProgress({ fraction, location }) {
  if (location) lastLoc = location
  curFrac = clamp01(fraction || 0)
  const slider = $('#progress-slider')
  if (slider) slider.value = curFrac
  $('#progress-label').textContent = pct(curFrac)
  if (!armed) paint(curFrac)
}

// Метки глав: TOC-ссылка → индекс секции → доля начала секции.
export function buildChapterMarks() {
  marks = []
  const ticks = $('#chapter-ticks')
  if (ticks) ticks.innerHTML = ''
  if (!view?.book) return
  let fractions = []
  try { fractions = view.getSectionFractions() || [] } catch { return }
  if (!fractions.length) return

  const seen = new Set()
  const walk = (items, top) => {
    for (const it of items || []) {
      // Вложенные пункты оглавления на шкалу не выносим — на телефоне это
      // превращается в частокол из десятков рисок вместо ориентиров.
      if (top && it.href) {
        let idx = null
        try { idx = view.book.resolveHref(it.href)?.index } catch { idx = null }
        const f = typeof idx === 'number' ? fractions[idx] : null
        if (typeof f === 'number' && !seen.has(idx)) {
          seen.add(idx)
          marks.push({ fraction: clamp01(f), label: (it.label || '').trim() || '—' })
        }
      }
      if (it.subitems?.length) walk(it.subitems, false)
    }
  }
  walk(view.book.toc || [], true)
  // Оглавления нет (частый случай у самиздата) — рисуем границы секций.
  if (!marks.length) {
    fractions.slice(1, -1).forEach((f, i) => marks.push({ fraction: clamp01(f), label: `Часть ${i + 2}` }))
  }
  marks.sort((a, b) => a.fraction - b.fraction)

  if (!ticks) return
  // Риски ближе 6 px друг к другу сливаются в сплошную серую полосу и перестают
  // быть ориентирами — у длинных книг глав больше, чем пикселей на треке.
  const trackW = $('#progress-track')?.getBoundingClientRect().width || 0
  const minGap = trackW > 0 ? 6 / trackW : 0.02
  const frag = document.createDocumentFragment()
  let lastDrawn = -1
  for (const m of marks) {
    if (m.fraction <= 0.001 || m.fraction >= 0.999) continue
    if (lastDrawn >= 0 && m.fraction - lastDrawn < minGap) continue
    lastDrawn = m.fraction
    const i = document.createElement('i')
    i.style.left = (m.fraction * 100) + '%'
    i.title = m.label
    frag.append(i)
  }
  ticks.append(frag)
  paint(curFrac)
}

// ===================== жесты =====================
function fracFromX(clientX) {
  const r = $('#progress-track').getBoundingClientRect()
  return r.width ? clamp01((clientX - r.left) / r.width) : 0
}

function arm(f) {
  armed = true
  dragFrac = f
  $('#progress-wrap').classList.remove('pending')
  $('#progress-wrap').classList.add('armed')
  try { navigator.vibrate?.(12) } catch { /* вибро есть не везде */ }
  paint(f)
  showBubble(f)
}

function cancelHold() {
  clearTimeout(holdTimer); holdTimer = null
  activeId = null
  $('#progress-wrap')?.classList.remove('pending')
  if (armed) {
    armed = false
    $('#progress-wrap').classList.remove('armed')
    hideBubble()
    paint(curFrac)
  }
}

function onDown(e) {
  if (activeId !== null) return
  activeId = e.pointerId
  startX = e.clientX; startY = e.clientY
  try { $('#progress-wrap').setPointerCapture(e.pointerId) } catch { /* ignore */ }
  // Мышью системного жеста нет — там ждать удержания незачем.
  if (e.pointerType === 'mouse') { arm(fracFromX(e.clientX)); return }
  // Пока идёт отсчёт удержания — видимый отклик, иначе первые 320 мс шкала
  // выглядит мёртвой и непонятно, что её вообще надо держать.
  $('#progress-wrap').classList.add('pending')
  holdTimer = setTimeout(() => { holdTimer = null; arm(fracFromX(startX)) }, HOLD_MS)
}

function onMove(e) {
  if (e.pointerId !== activeId) return
  if (armed) {
    dragFrac = fracFromX(e.clientX)
    paint(dragFrac); showBubble(dragFrac)
    e.preventDefault()
    return
  }
  // Ещё не проснулись: заметный сдвиг = это свайп (в том числе системный
  // «снизу вверх»), а не намерение перематывать. Снимаем ожидание.
  if (Math.abs(e.clientX - startX) > MOVE_TOL || Math.abs(e.clientY - startY) > MOVE_TOL) cancelHold()
}

function onUp(e) {
  if (e.pointerId !== activeId) return
  const wasArmed = armed
  const f = wasArmed ? fracFromX(e.clientX) : 0
  cancelHold()
  if (wasArmed && view) { curFrac = f; paint(f); view.goToFraction(f) }
}

export function initProgressBar() {
  const wrap = $('#progress-wrap')
  if (!wrap) return
  wrap.addEventListener('pointerdown', onDown)
  wrap.addEventListener('pointermove', onMove, { passive: false })
  wrap.addEventListener('pointerup', onUp)
  wrap.addEventListener('pointercancel', cancelHold)
  wrap.addEventListener('lostpointercapture', () => { if (!armed) cancelHold() })
  // Клавиатура: шкала фокусируется, стрелки двигают позицию (жест здесь ни при чём).
  wrap.addEventListener('keydown', (e) => {
    if (!view) return
    const step = { ArrowLeft: -0.01, ArrowRight: 0.01, PageDown: 0.05, PageUp: -0.05 }[e.key]
    let target = null
    if (step != null) target = clamp01(curFrac + step)
    else if (e.key === 'Home') target = 0
    else if (e.key === 'End') target = 1
    if (target == null) return
    e.preventDefault(); e.stopPropagation()
    curFrac = target; paint(target); view.goToFraction(target)
  })
}
