// Библиотека: список книг, ingest/upload, ReadEra-sync, тема библиотеки.
import { $, escapeHtml } from './core/dom.js'
import { api } from './core/api.js'
import { prefs, savePrefs } from './core/prefs.js'
import { libWorks, libCalibre, libProgress, libUpdated, libMonitored,
         setLibWorks, setLibCalibre, setLibProgress, setLibUpdated, setLibMonitored } from './core/state.js'
import { openReader } from './reader-core.js'
import { openBookPage, bookPageMeta } from './book-page.js'

// ===================== БИБЛИОТЕКА =====================
export async function loadLibrary() {
  setLibWorks(await api.get('/api/library'))
  // Один батч-запрос вместо N последовательных (раньше книги появлялись через 3-5с).
  const [monitored, progAll] = await Promise.all([
    api.get('/api/monitored').catch(() => []),
    api.get('/api/progress').catch(() => ({})),
  ])
  setLibUpdated(new Set(monitored.filter((m) => m.has_update && m.work_id).map((m) => m.work_id)))
  setLibMonitored(new Set(monitored.filter((m) => m.work_id).map((m) => m.work_id)))
  setLibProgress(progAll || {})
  // Единый класс «недавняя активность»: и обновление глав, и открытие/чтение
  // бампят work.updated_at на бэке. Сортируем строго по свежести — самое свежее
  // событие вверху (раньше has_update принудительно поднимались над недавно
  // читанными, из-за чего порядок по времени ломался — убрано).
  libWorks.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0))
  // Загружаем Calibre один раз (фоном, не блокируем рендер)
  api.get('/api/calibre/books').then(books => { setLibCalibre(books || []) }).catch(() => {})
  applyLibFilter('')
}

export function applyLibFilter(q) {
  const norm = (s) => (s || '').toLowerCase()
  const match = (s) => norm(s).includes(norm(q))
  const grid = $('#book-grid')
  grid.innerHTML = ''
  // Свои книги: показываем всегда (с фильтром если есть)
  const filtered = q
    ? libWorks.filter(w => match(w.title) || match(w.author))
    : libWorks
  for (const w of filtered) {
    grid.append(bookCard(w, libProgress[w.id] || 0, libUpdated.has(w.id)))
  }
  // Calibre: показываем только при активном фильтре (и только не импортированные)
  if (q && libCalibre.length) {
    const importedIds = new Set(libWorks.map(w => w.calibre_id).filter(Boolean))
    const calFiltered = libCalibre.filter(
      b => !importedIds.has(b.calibre_id) && (match(b.title) || match(b.authors))
    )
    for (const b of calFiltered) grid.append(calibreCard(b))
  }
  $('#lib-empty').hidden = grid.children.length > 0
}

// ===================== Hover-панель (только десктоп/мышь) =====================
const CAN_HOVER = window.matchMedia('(hover: hover) and (pointer: fine)').matches
let hoverEl = null
let hoverHideT = null

function ensureHoverEl() {
  if (hoverEl) return hoverEl
  hoverEl = document.createElement('div')
  hoverEl.id = 'lib-hover'
  hoverEl.className = 'lib-hover'
  hoverEl.hidden = true
  hoverEl.addEventListener('mouseenter', () => clearTimeout(hoverHideT))
  hoverEl.addEventListener('mouseleave', hideHover)
  document.body.append(hoverEl)
  return hoverEl
}

function hideHover() {
  clearTimeout(hoverHideT)
  hoverHideT = setTimeout(() => { if (hoverEl) hoverEl.hidden = true }, 120)
}

function hideHoverNow() {
  clearTimeout(hoverHideT)
  if (hoverEl) hoverEl.hidden = true
}

function showHover(card, w) {
  // Библиотека не на экране (открыта книга/читалка) — панель не показываем.
  if ($('#library').hidden || !card.isConnected) return
  clearTimeout(hoverHideT)
  const el = ensureHoverEl()
  const { chipsHtml, badgesHtml, factsText } = bookPageMeta(w)
  el.innerHTML = `
    <div class="lh-title">${escapeHtml(w.title || 'Без названия')}</div>
    <div class="lh-author">${escapeHtml(w.author || '')}</div>
    ${badgesHtml ? `<div class="lh-badges">${badgesHtml}</div>` : ''}
    ${factsText ? `<div class="lh-facts">${escapeHtml(factsText)}</div>` : ''}
    ${chipsHtml ? `<div class="lh-chips">${chipsHtml}</div>` : ''}
    <div class="lh-actions">
      <button class="btn-primary lh-read">📖 Читать</button>
      <button class="btn-ghost lh-open">Подробнее</button>
    </div>`
  el.querySelector('.lh-read').addEventListener('click', (e) => { e.stopPropagation(); el.hidden = true; openReader(w) })
  el.querySelector('.lh-open').addEventListener('click', (e) => { e.stopPropagation(); el.hidden = true; openBookPage(w) })
  // Позиционируем справа от карточки, если влезает, иначе слева.
  el.hidden = false
  const r = card.getBoundingClientRect()
  const pw = el.offsetWidth, ph = el.offsetHeight
  const gap = 10
  let left = r.right + gap
  if (left + pw > window.innerWidth - 8) left = r.left - gap - pw
  if (left < 8) left = Math.max(8, (window.innerWidth - pw) / 2)
  let top = r.top
  if (top + ph > window.innerHeight - 8) top = Math.max(8, window.innerHeight - 8 - ph)
  el.style.left = `${Math.round(left)}px`
  el.style.top = `${Math.round(top)}px`
}

function attachHover(card, w) {
  if (!CAN_HOVER) return
  let enterT = null
  card.addEventListener('mouseenter', () => {
    hideHoverNow()                       // мгновенно убрать панель прошлой карточки
    enterT = setTimeout(() => showHover(card, w), 350)
  })
  card.addEventListener('mouseleave', () => { clearTimeout(enterT); hideHover() })
}

function bookCard(w, ratio, hasUpdate) {
  const card = document.createElement('div')
  const readState = ratio >= 0.98 ? 'read' : ratio > 0 ? 'partial' : 'unread'
  card.className = ['book-card', readState, hasUpdate ? 'has-update' : ''].filter(Boolean).join(' ')
  const pct = Math.round((ratio || 0) * 100)
  const fallback = `<span class="cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
  // Всегда запрашиваем /cover: если обложки нет, бэкенд лениво сгенерирует её
  // ИИ и вернёт картинку. Пока грузится/если не вышло — виден текстовый фолбэк.
  const cover = `<img src="/api/reader/${w.id}/cover?v=${w.cover_v||0}" alt="" loading="lazy" decoding="async" onerror="this.remove()" />${fallback}`
  const badge = hasUpdate ? '<span class="upd-badge" title="Есть новые главы">обновление</span>' : ''
  card.innerHTML = `
    <div class="book-cover">${cover}${badge}<button class="book-del-btn" title="Удалить книгу" aria-label="Удалить">✕</button></div>
    <div class="book-meta">
      <div class="b-title">${escapeHtml(w.title || 'Без названия')}</div>
      <div class="b-author">${escapeHtml(w.author || '')}</div>
    </div>
    <div class="book-progress"><i style="width:${ratio >= 1 ? 100 : (ratio > 0 ? Math.min(pct, 93) : 0)}%"></i></div>`
  card.addEventListener('click', () => { hideHoverNow(); openBookPage(w) })
  attachHover(card, w)
  card.querySelector('.book-del-btn').addEventListener('click', async (e) => {
    e.stopPropagation()
    if (!confirm(`Удалить «${w.title || 'книгу'}»?`)) return
    card.style.opacity = '0.4'; card.style.pointerEvents = 'none'
    try {
      const r = await fetch(`/api/library/${w.id}`, { method: 'DELETE' })
      if (r.ok) card.remove()
      else { card.style.opacity = ''; card.style.pointerEvents = ''; alert('Ошибка удаления') }
    } catch { card.style.opacity = ''; card.style.pointerEvents = '' }
  })
  return card
}

function calibreCard(b) {
  const card = document.createElement('div')
  card.className = 'book-card'
  const fallback = `<span class="cover-fallback">${escapeHtml(b.title || 'Без названия')}</span>`
  const cover = b.has_cover
    ? `<img src="/api/calibre/${b.calibre_id}/cover" alt="" onerror="this.remove()" />${fallback}`
    : fallback
  card.innerHTML = `
    <div class="book-cover" style="position:relative">${cover}<span class="calibre-badge">Calibre</span></div>
    <div class="book-meta">
      <div class="b-title">${escapeHtml(b.title || 'Без названия')}</div>
      <div class="b-author">${escapeHtml(b.authors || '')}</div>
    </div>
    <div class="book-progress"><i style="width:0%"></i></div>`
  card.addEventListener('click', async () => {
    card.style.opacity = '0.5'; card.style.pointerEvents = 'none'
    try {
      const work = await api.post(`/api/calibre/import/${b.calibre_id}`, {})
      await loadLibrary()
      openReader(work)
    } catch (err) {
      card.style.opacity = ''; card.style.pointerEvents = ''
      alert('Не удалось открыть книгу из Calibre: ' + err.message)
    }
  })
  return card
}

// Добавление по ссылке или названию (/api/ingest): URL → адаптеры/FanFicFare,
// название → поиск в бесплатных агрегаторах (searchfloor/readli).
$('#ingest-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const q = $('#ingest-input').value.trim()
  if (!q) return
  const status = $('#ingest-status')
  const isUrl = /^https?:\/\//i.test(q)
  status.hidden = false; status.classList.remove('error')
  status.textContent = isUrl ? 'Скачиваю…' : 'Ищу по названию…'
  try {
    const work = await api.post('/api/ingest', { query: q })
    status.textContent = 'Готово: ' + (work.title || 'книга добавлена')
    $('#ingest-input').value = ''
    await loadLibrary()
  } catch (err) {
    status.classList.add('error')
    status.textContent = 'Не удалось добавить: ' + err.message.slice(0, 200)
  }
})

// Ручная загрузка файла (работает уже на этапе 1).
$('#upload-input').addEventListener('change', async (e) => {
  const file = e.target.files[0]
  if (!file) return
  const status = $('#ingest-status')
  status.hidden = false; status.classList.remove('error'); status.textContent = 'Загружаю файл…'
  const fd = new FormData(); fd.append('file', file)
  try {
    const r = await fetch('/api/library/upload', { method: 'POST', body: fd })
    if (!r.ok) throw new Error(await r.text())
    status.textContent = 'Файл добавлен.'
    await loadLibrary()
  } catch (err) {
    status.classList.add('error'); status.textContent = 'Ошибка загрузки: ' + err.message.slice(0, 160)
  }
  e.target.value = ''
})

// Синхронизация с ReadEra (импорт прогресса из бэкапа + экспорт веб-прогресса).
$('#readera-sync').addEventListener('click', async () => {
  const status = $('#ingest-status')
  status.hidden = false; status.classList.remove('error'); status.textContent = 'Синхронизирую с ReadEra…'
  try {
    const r = await api.post('/api/readera/sync', {})
    const imp = r.import || {}, exp = r.export || {}
    let msg = `ReadEra: импортировано позиций — ${imp.updated ?? 0}`
    if (!imp.ok && imp.reason) msg += ` (${imp.reason})`
    if (exp.patched) msg += `; для restore в ReadEra создан файл ${exp.restore_file}`
    status.textContent = msg
    await loadLibrary()
  } catch (err) {
    status.classList.add('error'); status.textContent = 'Sync ошибка: ' + err.message.slice(0, 160)
  }
})

$('#lib-filter').addEventListener('input', (e) => {
  applyLibFilter(e.target.value.trim())
})

// ===================== Тема в библиотеке =====================
const LIB_THEMES = ['day', 'sepia', 'grey', 'dusk', 'night', 'terminal', 'black']
function updateLibThemeBtn() {
  const isDark = ['dusk', 'night', 'terminal', 'black'].includes(prefs.theme)
  const btn = document.querySelector('#lib-theme-btn')
  if (btn) btn.title = isDark ? 'Тема: тёмная' : 'Тема: светлая'
}
updateLibThemeBtn()
document.querySelector('#lib-theme-btn')?.addEventListener('click', () => {
  const i = LIB_THEMES.indexOf(prefs.theme)
  prefs.theme = LIB_THEMES[(i + 1) % LIB_THEMES.length]
  document.documentElement.dataset.theme = prefs.theme
  savePrefs()
  updateLibThemeBtn()
})
