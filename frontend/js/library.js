// Библиотека: список книг, ingest/upload, ReadEra-sync, тема библиотеки.
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { prefs, savePrefs } from './core/prefs.js'
import { currentWork } from './core/state.js'
import { libWorks, libCalibre, libProgress, libUpdated, libMonitored,
         setLibWorks, setLibCalibre, setLibProgress, setLibUpdated, setLibMonitored } from './core/state.js'
import { openReader } from './reader-core.js'
import { openBookPage, bookPageMeta } from './book-page.js'
import { isOffline } from './core/offline.js'

// ---- Ленивая подгрузка обложек ----
// Раньше карточки рендерились с готовым <img src>, и браузер разом запрашивал
// /cover для ВСЕХ ~1400 книг. Это забивало бэкенд и пул коннектов Postgres, из-за
// чего открытие книги «висло». Теперь src проставляется только когда карточка
// подходит к вьюпорту → в полёте лишь обложки видимых книг.
let _coverIO = null

function resetCoverObserver() {
  if (_coverIO) _coverIO.disconnect()
  if (!('IntersectionObserver' in window)) { _coverIO = null; return }
  _coverIO = new IntersectionObserver((entries, obs) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue
      const img = e.target
      obs.unobserve(img)
      if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src }
    }
  }, { rootMargin: '400px 0px' })  // подгружаем чуть раньше появления
}

function observeCovers(grid) {
  const imgs = grid.querySelectorAll('img[data-src]')
  if (!_coverIO) {  // нет IntersectionObserver — грузим сразу (деградация)
    for (const img of imgs) { img.src = img.dataset.src; delete img.dataset.src }
    return
  }
  for (const img of imgs) _coverIO.observe(img)
}

// Текстовая заглушка обложки (видна пока картинка грузится / если её нет).
function coverFallback(title) {
  return `<span class="cover-fallback">${escapeHtml(title || 'Без названия')}</span>`
}

// <img> обложки с ленивой подгрузкой (data-src ставится при подходе к вьюпорту)
// и текстовым фолбэком под ним.
function coverImg(src, title) {
  return `<img data-src="${src}" alt="" loading="lazy" decoding="async" onerror="this.remove()" />${coverFallback(title)}`
}

// ===================== БИБЛИОТЕКА =====================
// Снимок числа глав по книгам — чтобы заметить фоновую авто-докачку и показать toast.
// В памяти (не localStorage): при перезагрузке страницы пере-сеется, без устаревших пушей.
let _chapSnap = null
function detectAutoUpdates() {
  const cur = new Map(libWorks.map((w) => [w.id, w.chapters_count || 0]))
  if (_chapSnap === null) { _chapSnap = cur; return }  // первый заход — сеем базу без toast
  const grown = []
  for (const [id, n] of cur) {
    const prev = _chapSnap.get(id)
    // Пропускаем текущую открытую книгу — по ней уже был toast ручного обновления.
    if (prev !== undefined && n > prev && id !== currentWork?.id) {
      const w = libWorks.find((x) => x.id === id)
      grown.push({ title: w?.title || 'Книга', n })
    }
  }
  _chapSnap = cur
  if (grown.length === 1) toast(`«${grown[0].title}» обновлена сама — ${grown[0].n} гл.`, 'ok', 6000)
  else if (grown.length > 1) toast(`Обновилось книг: ${grown.length} — есть новые главы`, 'ok', 6000)
}

export async function loadLibrary() {
  setLibWorks(await api.get('/api/library'))
  detectAutoUpdates()
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
  // Сохраняем активный фильтр (клик по автору/серии, ручной ввод) при перезагрузке
  // библиотеки — иначе возврат со страницы книги (popstate→loadLibrary) сбрасывал бы его.
  applyLibFilter((($('#lib-filter') || {}).value || '').trim())
}

export function applyLibFilter(q) {
  const norm = (s) => (s || '').toLowerCase()
  const match = (s) => norm(s).includes(norm(q))
  const grid = $('#book-grid')
  grid.innerHTML = ''
  resetCoverObserver()  // сбрасываем наблюдатель под новый набор карточек
  // Свои книги: показываем всегда (с фильтром если есть)
  const filtered = q
    ? libWorks.filter(w => match(w.title) || match(w.author) || match(w.series))
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
  observeCovers(grid)  // подгрузим обложки только для видимых карточек
}

// Программно применить фильтр (клик по автору/серии). Значение кладём в поле
// фильтра — оно же служит индикатором и «сбросом» (очистить поле → весь список).
export function filterBy(value) {
  const inp = $('#lib-filter')
  if (inp) inp.value = value || ''
  applyLibFilter((value || '').trim())
  window.scrollTo({ top: 0, behavior: 'smooth' })
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
    <div class="lh-author${w.author ? ' lh-link' : ''}" data-flt="author" title="Показать все книги автора">${escapeHtml(w.author || '')}</div>
    ${w.series ? `<div class="lh-series lh-link" data-flt="series" title="Показать всю серию">📚 ${escapeHtml(w.series)}${w.series_index ? ' #' + w.series_index : ''}</div>` : ''}
    ${badgesHtml ? `<div class="lh-badges">${badgesHtml}</div>` : ''}
    ${factsText ? `<div class="lh-facts">${escapeHtml(factsText)}</div>` : ''}
    ${chipsHtml ? `<div class="lh-chips">${chipsHtml}</div>` : ''}
    <div class="lh-actions">
      <button class="btn-primary lh-read">📖 Читать</button>
      <button class="btn-ghost lh-open">Подробнее</button>
    </div>`
  el.querySelector('.lh-read').addEventListener('click', (e) => { e.stopPropagation(); el.hidden = true; openReader(w) })
  el.querySelector('.lh-open').addEventListener('click', (e) => { e.stopPropagation(); el.hidden = true; openBookPage(w) })
  el.querySelectorAll('[data-flt]').forEach((elx) => {
    elx.addEventListener('click', (e) => {
      e.stopPropagation(); el.hidden = true
      filterBy(elx.dataset.flt === 'series' ? w.series : w.author)
    })
  })
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
  // Дочитано (read) — полоса всегда 100%. foliate почти никогда не даёт ровно
  // 1.0 на последней странице (типично 0.9999…/0.98), поэтому «дочитано» —
  // это порог readState (>=0.98), а не строгое ratio>=1, иначе дочитанная книга
  // застревала на визуальном максимуме недочитанной (93%).
  const done = readState === 'read'
  // Плашку «обновление» не показываем на дочитанной книге: ficbook-лента метит
  // has_update при любой активности автора (не только новые главы), и дочитанная
  // книга висела с ложной плашкой. Когда реально докачается новая глава,
  // непрочитанный контент уронит ratio ниже порога — плашка вернётся сама.
  const showUpdate = hasUpdate && !done
  card.className = ['book-card', readState, showUpdate ? 'has-update' : ''].filter(Boolean).join(' ')
  const pct = Math.round((ratio || 0) * 100)
  // Всегда запрашиваем /cover: если обложки нет, бэкенд лениво сгенерирует её
  // ИИ и вернёт картинку. Пока грузится/если не вышло — виден текстовый фолбэк.
  const cover = coverImg(`/api/reader/${w.id}/cover?v=${w.cover_v||0}`, w.title)
  const badge = showUpdate ? '<span class="upd-badge" title="Есть новые главы">обновление</span>' : ''
  const offBadge = isOffline(w.id) ? '<span class="offline-badge" title="Доступна офлайн">офлайн</span>' : ''
  card.innerHTML = `
    <div class="book-cover">${cover}${badge}${offBadge}<button class="book-del-btn" title="Удалить книгу" aria-label="Удалить">✕</button></div>
    <div class="book-meta">
      <div class="b-title">${escapeHtml(w.title || 'Без названия')}</div>
      <div class="b-author${w.author ? ' b-link' : ''}" data-flt="author">${escapeHtml(w.author || '')}</div>
      ${w.series ? `<div class="b-series b-link" data-flt="series" title="Показать всю серию">📚 ${escapeHtml(w.series)}${w.series_index ? ' #' + w.series_index : ''}</div>` : ''}
    </div>
    <div class="book-progress"><i style="width:${done ? 100 : (ratio > 0 ? Math.min(pct, 93) : 0)}%"></i></div>`
  // Доступность: карточка — это кнопка «открыть книгу». Делаем её достижимой с
  // клавиатуры (Tab) и активируемой Enter/Space (focus-visible уже стилизован).
  card.tabIndex = 0
  card.setAttribute('role', 'button')
  card.setAttribute('aria-label',
    `${w.title || 'Книга'}${w.author ? ', ' + w.author : ''}`)
  const openThis = () => { hideHoverNow(); openBookPage(w) }
  card.addEventListener('click', openThis)
  card.addEventListener('keydown', (e) => {
    // Не перехватываем активацию вложенной кнопки удаления.
    if (e.target !== card) return
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openThis() }
  })
  attachHover(card, w)
  card.querySelectorAll('[data-flt]').forEach((el) => {
    el.addEventListener('click', (e) => {
      const val = el.dataset.flt === 'series' ? w.series : w.author
      if (!val) return
      e.stopPropagation(); hideHoverNow(); filterBy(val)
    })
  })
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
  const cover = b.has_cover
    ? coverImg(`/api/calibre/${b.calibre_id}/cover`, b.title)
    : coverFallback(b.title)
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

// Общий статус-строки внизу формы добавления: показать сообщение (или ошибку).
function setIngestStatus(msg, { error = false } = {}) {
  const status = $('#ingest-status')
  status.hidden = false
  status.classList.toggle('error', error)
  status.textContent = msg
}

// Добавление по ссылке или названию (/api/ingest): URL → адаптеры/FanFicFare,
// название → поиск в бесплатных агрегаторах (searchfloor/readli).
$('#ingest-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const q = $('#ingest-input').value.trim()
  if (!q) return
  const isUrl = /^https?:\/\//i.test(q)
  setIngestStatus(isUrl ? 'Скачиваю…' : 'Ищу по названию…')
  try {
    const work = await api.post('/api/ingest', { query: q })
    setIngestStatus('Готово: ' + (work.title || 'книга добавлена'))
    $('#ingest-input').value = ''
    await loadLibrary()
  } catch (err) {
    setIngestStatus('Не удалось добавить: ' + err.message.slice(0, 200), { error: true })
  }
})

// Ручная загрузка файла (работает уже на этапе 1).
$('#upload-input').addEventListener('change', async (e) => {
  const file = e.target.files[0]
  if (!file) return
  setIngestStatus('Загружаю файл…')
  const fd = new FormData(); fd.append('file', file)
  try {
    const r = await fetch('/api/library/upload', { method: 'POST', body: fd })
    if (!r.ok) throw new Error(await r.text())
    setIngestStatus('Файл добавлен.')
    await loadLibrary()
  } catch (err) {
    setIngestStatus('Ошибка загрузки: ' + err.message.slice(0, 160), { error: true })
  }
  e.target.value = ''
})

// Синхронизация с ReadEra (импорт прогресса из бэкапа + экспорт веб-прогресса).
$('#readera-sync').addEventListener('click', async () => {
  setIngestStatus('Синхронизирую с ReadEra…')
  try {
    const r = await api.post('/api/readera/sync', {})
    const imp = r.import || {}, exp = r.export || {}
    let msg = `ReadEra: импортировано позиций — ${imp.updated ?? 0}`
    if (!imp.ok && imp.reason) msg += ` (${imp.reason})`
    if (exp.patched) msg += `; для restore в ReadEra создан файл ${exp.restore_file}`
    setIngestStatus(msg)
    await loadLibrary()
  } catch (err) {
    setIngestStatus('Sync ошибка: ' + err.message.slice(0, 160), { error: true })
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
