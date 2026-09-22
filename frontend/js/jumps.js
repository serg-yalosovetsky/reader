// История переходов по книге: кнопка «Назад» в читалке и список позиций по
// долгому нажатию (serg/tasks#1078).
//
// Зачем: позиция чтения — ОДНА строка на книгу (Progress), её перезаписывает
// любой релокейт. Книга открыта в двух вкладках, во второй она стоит на первой
// странице — её сохранение затирает главу 100, и вернуться некуда. То же самое
// делает любой прыжок: по оглавлению, по ссылке-сноске, перемоткой шкалы.
//
// Две памяти, намеренно:
//  * локальный стек этой вкладки — мгновенный «Назад» без сети, LIFO;
//  * серверная история (/api/progress/{id}/history) — переживает перезагрузку
//    вкладки и видна с другого устройства; туда же сервер САМ кладёт позицию,
//    которую затёрла чужая вкладка (см. backend/app/routers/progress.py).
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { view, currentWork, lastCfi, lastAnchor, lastFraction } from './core/state.js'
import { chapterTitle } from './chrome.js'
import { gotoPosition } from './core/position.js'

// Глубже этого локальный стек не растёт: «Назад» — рабочий инструмент на
// несколько шагов, а не журнал чтения. Дальше — серверная история.
const MAX_LOCAL = 50

// Тот же порог, что у сервера (JUMP_RATIO в backend/app/routers/progress.py):
// насколько должна измениться доля, чтобы сохранение считалось прыжком и
// прежняя позиция уехала в историю. Держим копию, потому что решение сервера
// ответом PUT не возвращается, а кнопка «Назад» обязана ожить сразу.
const JUMP_RATIO = 0.005

let stack = []          // прыжки этой вкладки, новые в конце
let savedRatio = 0      // доля, которую сервер видел в прошлом сохранении
let serverHasRows = false  // знает ли сервер о прошлых позициях этой книги

// Текущая позиция читалки в том же виде, в каком её хранит сервер.
function currentPosition() {
  return {
    ratio: lastFraction || 0,
    locator: lastCfi || '',
    text_anchor: lastAnchor || '',
    chapter: chapterTitle(),
  }
}

function isEmpty(pos) {
  return !pos || (!pos.locator && !pos.text_anchor && !pos.ratio)
}

// Кнопка «Назад» активна, только когда есть куда возвращаться: неактивная
// кнопка честнее пустой панели после нажатия.
function syncBackBtn() {
  const btn = $('#jump-back-btn')
  if (!btn) return
  const can = stack.length > 0 || serverHasRows
  btn.disabled = !can
  btn.title = can
    ? 'Вернуться к прошлой позиции (удерживайте — список переходов)'
    : 'Переходов пока не было'
}

// Вызывается при открытии книги: стек чужой книги к новой отношения не имеет.
export async function resetJumps(workId, startRatio = 0) {
  stack = []
  savedRatio = startRatio || 0
  serverHasRows = false
  syncBackBtn()
  if (!workId) return
  try {
    const rows = await api.get(`/api/progress/${workId}/history`)
    serverHasRows = Array.isArray(rows) && rows.length > 0
  } catch (e) {
    // Нет связи — локальный стек всё равно работает, но молчать нельзя:
    // иначе «Назад» выглядит сломанным без объяснения.
    logErr('история переходов не загрузилась', e)
  }
  syncBackBtn()
}

// Сохранение прогресса ушло далеко от прошлого сохранения — значит сервер сам
// положил прежнюю позицию в историю (в том числе когда её затёрла эта вкладка,
// открытая в начале книги). Кнопке «Назад» есть что предложить.
//
// Считаем именно по сохранениям: соседние релокейты во время восстановления
// позиции прыгают сами по себе, и по ним кнопка зажигалась на пустой истории.
export function noteProgressSaved(ratio) {
  const jumped = Math.abs(savedRatio - (ratio || 0)) >= JUMP_RATIO
  savedRatio = ratio || 0
  if (!jumped || serverHasRows) return
  serverHasRows = true
  syncBackBtn()
}

// Запомнить ТЕКУЩУЮ позицию перед прыжком. reason: jump | toc | search |
// bookmark | seek | link — попадает в подпись строки в списке.
export function recordJump(reason = 'jump') {
  const pos = currentPosition()
  if (isEmpty(pos)) return
  const last = stack[stack.length - 1]
  if (last && last.locator === pos.locator && last.text_anchor === pos.text_anchor) return
  stack.push({ ...pos, reason, created_at: new Date().toISOString(), local: true })
  if (stack.length > MAX_LOCAL) stack.shift()
  syncBackBtn()
  const id = currentWork?.id
  if (!id) return
  serverHasRows = true
  api.post(`/api/progress/${id}/history`, { ...pos, reason })
    .catch((e) => logErr('не сохранил переход в историю', e))
}

// Положить позицию в серверную историю и тут же оживить кнопку «Назад».
// Отдельная функция, потому что писать в историю умеет не только этот модуль:
// читалка кладёт сюда позицию, с которой ушла, открываясь на выбранной главе.
export function pushServerJump(pos, reason = 'jump') {
  if (isEmpty(pos)) return Promise.resolve()
  const id = currentWork?.id
  if (!id) return Promise.resolve()
  serverHasRows = true
  syncBackBtn()
  return api.post(`/api/progress/${id}/history`, {
    ratio: pos.ratio || 0,
    locator: pos.locator || '',
    text_anchor: pos.text_anchor || '',
    chapter: pos.chapter || '',
    reason,
  }).catch((e) => logErr('не сохранил переход в историю', e))
}

// Перейти к сохранённой позиции. record=true — текущую позицию тоже положить
// в историю (чтобы из любой точки списка можно было вернуться обратно).
async function goToPos(pos, { record = true } = {}) {
  if (!view) return false
  if (isEmpty(pos)) {
    // Запись без координат — возвращаться некуда. Молчание здесь
    // читалось бы как сломанная кнопка.
    toast('Эта запись без координат — переходить некуда', 'err')
    return false
  }
  if (record) {
    const cur = currentPosition()
    const id = currentWork?.id
    if (!isEmpty(cur) && id) {
      api.post(`/api/progress/${id}/history`, { ...cur, reason: 'back' })
        .catch((e) => logErr('не сохранил обратный переход', e))
      serverHasRows = true
    }
  }
  const ok = await gotoPosition(view, pos)
  if (!ok) toast('Это место в книге больше не находится — возможно, её пересобрали', 'err')
  return ok
}

// Кнопка «Назад»: сначала прыжки этой вкладки (мгновенно), потом серверная
// история — в ней лежит и позиция, затёртая другой вкладкой.
export async function jumpBack() {
  const local = stack.pop()
  syncBackBtn()
  if (local) return goToPos(local)

  const id = currentWork?.id
  if (!id) return false
  let rows = []
  try {
    rows = await api.get(`/api/progress/${id}/history`)
  } catch (e) {
    logErr('история переходов не загрузилась', e)
    toast('Не удалось получить историю переходов', 'err')
    return false
  }
  const cur = currentPosition()
  // Первая запись, которая не совпадает с тем, где мы стоим сейчас: иначе
  // «Назад» никуда не ведёт и выглядит сломанным.
  const target = (rows || []).find(
    (r) => r.locator !== cur.locator || r.text_anchor !== cur.text_anchor,
  )
  serverHasRows = (rows || []).length > 0
  syncBackBtn()
  if (!target) {
    toast('Возвращаться некуда — переходов по этой книге не было', 'info')
    return false
  }
  return goToPos(target)
}

const REASONS = {
  toc: 'оглавление',
  search: 'поиск',
  bookmark: 'закладка',
  seek: 'перемотка',
  link: 'ссылка',
  open: 'открытие книги',
  back: 'возврат',
  overwrite: 'затёрто другой вкладкой',
  jump: 'переход',
}

function fmtWhen(iso) {
  if (!iso) return ''
  // Сервер отдаёт время в UTC, но без суффикса Z (naive datetime) — без него
  // браузер прочитал бы его как местное и показал «3 часа назад» как «сейчас».
  const norm = /[Z+]|-\d\d:\d\d$/.test(iso) ? iso : iso + 'Z'
  const d = new Date(norm)
  if (isNaN(d)) return ''
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins < 1) return 'только что'
  if (mins < 60) return `${mins} мин назад`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours} ч назад`
  return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

// Список для панели: серверная история + локальные прыжки, без дублей.
async function jumpList() {
  const id = currentWork?.id
  let rows = []
  if (id) {
    try {
      rows = (await api.get(`/api/progress/${id}/history`)) || []
    } catch (e) {
      logErr('история переходов не загрузилась', e)
    }
  }
  const seen = new Set()
  const all = []
  for (const r of [...stack].reverse().concat(rows)) {
    const key = `${r.locator}|${r.text_anchor}|${Math.round((r.ratio || 0) * 1000)}`
    if (seen.has(key)) continue
    seen.add(key)
    all.push(r)
  }
  return all
}

export async function renderJumpsPanel() {
  const list = $('#jumps-list')
  if (!list) return
  list.innerHTML = '<div class="jumps-empty">Загружаю…</div>'
  const rows = await jumpList()
  if (!rows.length) {
    list.innerHTML = '<div class="jumps-empty">Переходов по этой книге ещё не было.'
      + ' Сюда попадают места, из которых вы ушли: по оглавлению, по ссылке,'
      + ' перемоткой — и позиция, затёртая другой вкладкой.</div>'
    return
  }
  list.innerHTML = ''
  for (const r of rows) {
    const a = document.createElement('a')
    a.href = '#'
    a.className = 'jump-item'
    const pct = Math.round((r.ratio || 0) * 100)
    a.innerHTML = `<span class="jump-ch">${escapeHtml(r.chapter || 'Без главы')}</span>`
      + `<span class="jump-meta">${pct}% · ${escapeHtml(REASONS[r.reason] || r.reason || '')}`
      + `${r.created_at ? ' · ' + escapeHtml(fmtWhen(r.created_at)) : ''}</span>`
    a.addEventListener('click', async (ev) => {
      ev.preventDefault()
      const { closePanels } = await import('./navigation.js')
      closePanels()
      // Уходим из текущего места — оно тоже должно попасть в историю.
      await goToPos(r)
    })
    list.append(a)
  }
}

export async function clearJumps() {
  const id = currentWork?.id
  stack = []
  if (id) {
    try {
      await api.delete(`/api/progress/${id}/history`)
    } catch (e) {
      logErr('не очистил историю переходов', e)
      toast('Не удалось очистить историю', 'err')
      return
    }
  }
  serverHasRows = false
  syncBackBtn()
  renderJumpsPanel()
}

// Кнопка в тулбаре: короткое нажатие — назад, долгое — список переходов.
// Долгое нажатие, а не вторая кнопка: тулбар уже плотный, а список нужен реже.
const LONG_PRESS_MS = 520
const MOVE_TOL = 12

// Вкладку показали снова — за время в фоне в книге могли появиться новые
// прошлые позиции (их кладёт сервер, когда эту позицию кто-то затёр).
async function refreshFromServer() {
  const id = currentWork?.id
  if (!id || document.hidden) return
  try {
    const rows = await api.get(`/api/progress/${id}/history`)
    serverHasRows = Array.isArray(rows) && rows.length > 0
    syncBackBtn()
  } catch (e) {
    logErr('история переходов не обновилась', e)
  }
}

export function initJumps() {
  document.addEventListener('visibilitychange', refreshFromServer)
  const btn = $('#jump-back-btn')
  if (!btn) return
  let timer = null
  let longFired = false
  let sx = 0
  let sy = 0

  const openList = async () => {
    longFired = true
    const { openPanel } = await import('./navigation.js')
    openPanel('#jumps-panel')
    renderJumpsPanel()
  }
  const cancel = () => { clearTimeout(timer); timer = null }

  btn.addEventListener('pointerdown', (e) => {
    if (btn.disabled) return
    longFired = false
    sx = e.clientX; sy = e.clientY
    cancel()
    timer = setTimeout(openList, LONG_PRESS_MS)
  })
  btn.addEventListener('pointermove', (e) => {
    if (!timer) return
    if (Math.abs(e.clientX - sx) > MOVE_TOL || Math.abs(e.clientY - sy) > MOVE_TOL) cancel()
  })
  btn.addEventListener('pointerup', cancel)
  btn.addEventListener('pointercancel', () => { cancel(); longFired = false })
  btn.addEventListener('pointerleave', cancel)
  // Контекстное меню на долгий тап в мобильном Chrome перехватило бы жест.
  btn.addEventListener('contextmenu', (e) => { if (longFired) e.preventDefault() })
  btn.addEventListener('click', (e) => {
    e.preventDefault()
    if (longFired) { longFired = false; return }
    jumpBack()
  })
  // Клавиатура: список переходов без долгого нажатия (его пальцем не изобразишь
  // на Tab-навигации) — Alt+стрелка вниз, как у комбобокса.
  btn.addEventListener('keydown', (e) => {
    if (e.altKey && e.key === 'ArrowDown') { e.preventDefault(); openList() }
  })
  $('#jumps-clear')?.addEventListener('click', clearJumps)
}
