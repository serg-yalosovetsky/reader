// Библиотека: список книг, ingest/upload, ReadEra-sync, тема библиотеки.
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { prefs, savePrefs } from './core/prefs.js'
import { currentWork } from './core/state.js'
import { libWorks, libCalibre, libProgress, libUpdated, libMonitored,
         setLibWorks, setLibCalibre, setLibProgress, setLibUpdated, setLibMonitored } from './core/state.js'
import { openReader } from './reader-core.js'
import { openBookPage, bookPageMeta } from './book-page.js'
import { isOffline, offlineIds, removeBook, downloadBook } from './core/offline.js'

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
      // srcset ДО src: иначе браузер успевает начать грузить src, а потом
      // передумывает и тянет второй файл — двойной трафик на ровном месте.
      if (img.dataset.srcset) { img.srcset = img.dataset.srcset; delete img.dataset.srcset }
      if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src }
    }
  }, { rootMargin: '400px 0px' })  // подгружаем чуть раньше появления
}

function observeCovers(grid) {
  const imgs = grid.querySelectorAll('img[data-src]')
  if (!_coverIO) {  // нет IntersectionObserver — грузим сразу (деградация)
    for (const img of imgs) {
      if (img.dataset.srcset) { img.srcset = img.dataset.srcset; delete img.dataset.srcset }
      img.src = img.dataset.src; delete img.dataset.src
    }
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
function coverImg(src, title, srcset = '', sizes = '') {
  const responsive = srcset ? ` data-srcset="${srcset}" sizes="${sizes}"` : ''
  return `<img data-src="${src}"${responsive} alt="" loading="lazy" decoding="async" onerror="this.remove()" />${coverFallback(title)}`
}

// ===================== БИБЛИОТЕКА =====================
// Снимок числа глав по книгам — чтобы заметить фоновую авто-докачку и показать toast.
// В localStorage: in-memory снапшот пересеивался при каждой перезагрузке, и toast
// про фоновые докачки практически никогда не доживал до пользователя.
const SNAP_KEY = 'reader:chapSnap'
function loadSnap() {
  try { const v = JSON.parse(localStorage.getItem(SNAP_KEY)); return v ? new Map(v) : null }
  catch { return null }
}
function saveSnap(m) { try { localStorage.setItem(SNAP_KEY, JSON.stringify([...m])) } catch { /* quota */ } }
let _chapSnap = loadSnap()
function detectAutoUpdates() {
  const cur = new Map(libWorks.map((w) => [w.id, w.chapters_count || 0]))
  if (_chapSnap === null) { _chapSnap = cur; saveSnap(cur); return }  // первый заход — сеем базу без toast
  const grown = []
  for (const [id, n] of cur) {
    const prev = _chapSnap.get(id)
    // Пропускаем текущую открытую книгу — по ней уже был toast ручного обновления.
    if (prev !== undefined && n > prev && id !== currentWork?.id) {
      const w = libWorks.find((x) => x.id === id)
      grown.push({ id, title: w?.title || 'Книга', n })
    }
  }
  _chapSnap = cur
  saveSnap(cur)
  if (grown.length === 1) toast(`«${grown[0].title}» обновлена сама — ${grown[0].n} гл.`, 'ok', 6000)
  else if (grown.length > 1) toast(`Обновилось книг: ${grown.length} — есть новые главы`, 'ok', 6000)
  // Обновившиеся офлайн-копии освежаем в фоне: читалка открывает книги
  // cache-first, и без переустановки копии открывался бы старый файл.
  for (const g of grown) {
    if (isOffline(g.id)) removeBook(g.id).then(() => downloadBook(g.id)).catch(() => {})
  }
}

// Плашка в шапке: сколько подписок нашли обновление, но оно ещё не скачано.
// Видит и подписки без карточки (ficbook-сироты, у которых ещё нет work_id).
function renderPendingPill(monitored) {
  const pill = $('#pending-pill')
  if (!pill) return
  const n = (monitored || []).filter((m) => m.has_update).length
  pill.hidden = n === 0
  if (n) {
    pill.textContent = `⬇ ${n}`
    pill.title = `Обновлений ждут скачивания: ${n} — открыть «Аккаунты»`
    pill.onclick = () => $('#accounts-btn')?.click()
  }
}

// Последний успешно полученный список книг. Нужен, чтобы офлайн (или когда
// связь «есть, но мёртвая» и SW отдаёт отказ) библиотека вообще отрисовалась:
// раньше api.get('/api/library') без .catch ронял loadLibrary на первой строке,
// и пользователь не видел даже те книги, что сам сохранил офлайн.
const WORKS_KEY = 'reader:worksSnap'

function saveWorksSnap(works) {
  try { localStorage.setItem(WORKS_KEY, JSON.stringify(works)) } catch { /* quota */ }
}
function loadWorksSnap() {
  try {
    const v = JSON.parse(localStorage.getItem(WORKS_KEY) || 'null')
    return Array.isArray(v) ? v : null
  } catch { return null }
}

export async function loadLibrary() {
  let offlineMode = false
  try {
    const works = await api.get('/api/library')
    setLibWorks(works)
    saveWorksSnap(works)
  } catch (err) {
    const snap = loadWorksSnap()
    if (!snap) throw err            // и снимка нет — показать честную ошибку
    setLibWorks(snap)
    offlineMode = true
    const n = offlineIds().length
    toast(n ? `Нет сети — показываю сохранённое. Офлайн доступно книг: ${n}`
            : 'Нет сети — показываю последний список. Офлайн-копий пока нет', 'info', 6000)
  }
  document.body.classList.toggle('is-offline', offlineMode)
  detectAutoUpdates()
  // Один батч-запрос вместо N последовательных (раньше книги появлялись через 3-5с).
  const [monitored, progAll] = await Promise.all([
    api.get('/api/monitored').catch(() => []),
    api.get('/api/progress').catch(() => ({})),
  ])
  setLibUpdated(new Set(monitored.filter((m) => m.has_update && m.work_id).map((m) => m.work_id)))
  setLibMonitored(new Set(monitored.filter((m) => m.work_id).map((m) => m.work_id)))
  renderPendingPill(monitored)
  setLibProgress(progAll || {})
  // Единый класс «недавняя активность»: и обновление глав, и открытие/чтение
  // бампят work.updated_at на бэке. Сортируем строго по свежести — самое свежее
  // событие вверху (раньше has_update принудительно поднимались над недавно
  // читанными, из-за чего порядок по времени ломался — убрано).
  libWorks.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0))
  // Сохраняем активный фильтр (клик по автору/серии, ручной ввод) при перезагрузке
  // библиотеки — иначе возврат со страницы книги (popstate→loadLibrary) сбрасывал бы его.
  applyLibFilter((($('#lib-q') || {}).value || '').trim())
}

// Ссылка, а не поисковый запрос. Фильтровать по ней бессмысленно: в названиях
// URL не встречается, и список схлопнулся бы в пустоту у человека, который
// просто вставил ссылку на скачивание.
export function isUrlQuery(s) { return /^https?:\/\//i.test((s || '').trim()) }

const _norm = (s) => (s || '').toLowerCase()
const _hit = (s, q) => _norm(s).includes(_norm(q))

// Единый критерий «подходит под запрос» — один и тот же для отрисовки списка и
// для проверки «книга уже есть». Разъедься они, список показывал бы книгу, а
// Enter всё равно лез бы качать её из сети.
export function findLibMatches(q) {
  const s = (q || '').trim()
  if (!s) return { works: [], calibre: [] }
  const works = libWorks.filter(w => _hit(w.title, s) || _hit(w.author, s) || _hit(w.series, s))
  const importedIds = new Set(libWorks.map(w => w.calibre_id).filter(Boolean))
  const calibre = libCalibre.filter(
    b => !importedIds.has(b.calibre_id) && (_hit(b.title, s) || _hit(b.authors, s))
  )
  return { works, calibre }
}

// Каталог Calibre нужен только при активном фильтре: без фильтра чужие книги
// в списке не показываются вообще. Поэтому грузим его при первом поиске, а не
// на открытии библиотеки — иначе 214 КБ и (на холодном кэше сервера) до десяти
// секунд тратятся всегда, даже когда человек просто пришёл читать своё.
let calibreReq = null
function ensureCalibre() {
  if (calibreReq || libCalibre.length) return
  calibreReq = api.get('/api/calibre/books')
    .then((books) => {
      setLibCalibre(books || [])
      // Каталог пришёл уже после отрисовки — дорисуем, если запрос тот же.
      const q = (($('#lib-q') || {}).value || '').trim()
      if (q && !isUrlQuery(q)) applyLibFilter(q)
    })
    .catch(() => {})
}

export function applyLibFilter(q) {
  if (q) ensureCalibre()
  const hits = findLibMatches(q)
  const grid = $('#book-grid')
  grid.innerHTML = ''
  resetCoverObserver()  // сбрасываем наблюдатель под новый набор карточек
  // Свои книги: показываем всегда (с фильтром если есть)
  let filtered = q ? hits.works : libWorks
  // Офлайн: сохранённые книги — наверх. Остальные не прячем (видно, что есть в
  // библиотеке), но они уедут вниз и погаснут — см. .no-offline-copy в CSS.
  if (document.body.classList.contains('is-offline')) {
    filtered = [...filtered].sort((a, b) => (isOffline(b.id) ? 1 : 0) - (isOffline(a.id) ? 1 : 0))
  }
  for (const w of filtered) {
    grid.append(bookCard(w, libProgress[w.id] || 0, libUpdated.has(w.id)))
  }
  // Calibre: показываем только при активном фильтре (и только не импортированные)
  if (q) for (const b of hits.calibre) grid.append(calibreCard(b))
  $('#lib-empty').hidden = grid.children.length > 0
  observeCovers(grid)  // подгрузим обложки только для видимых карточек
  lastFilterApplied = q
}

// Что уже отрисовано. Перерисовка грида — это полторы тысячи карточек, и
// повторять её для того же запроса незачем: при наборе ссылки, например,
// фильтр остаётся пустым на каждом символе.
let lastFilterApplied = null
let filterTimer = null

// Применить фильтр немедленно, отменив отложенный.
function applyFilterNow(q) {
  clearTimeout(filterTimer)
  filterTimer = null
  if (q !== lastFilterApplied) applyLibFilter(q)
}

// Программно применить фильтр (клик по автору/серии). Значение кладём в поле
// фильтра — оно же служит индикатором и «сбросом» (очистить поле → весь список).
export function filterBy(value) {
  const inp = $('#lib-q')
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

// Деталь книги для панели по наведению. Список намеренно узкий (9 полей), а
// панель показывает жанры, рейтинг, статус, источник и объём — их там нет.
// Ключ — id книги, значение — ответ /api/library/{id}; null означает «запрос
// уже летит», чтобы одно наведение не порождало пачку одинаковых запросов.
const hoverDetail = new Map()

function prefetchDetail(id) {
  if (!id || hoverDetail.has(id)) return
  hoverDetail.set(id, null)
  api.get(`/api/library/${id}`)
    .then((full) => {
      hoverDetail.set(id, full)
      // Пока летел запрос, панель могла уже открыться на этой же книге — тогда
      // перерисуем её с фактами. Если навели на другую, трогать нечего.
      if (hoverEl && !hoverEl.hidden && hoverCard && hoverCard._w && hoverCard._w.id === id) {
        showHover(hoverCard, hoverCard._w)
      }
    })
    // Не смогли — забываем, чтобы следующее наведение попробовало снова.
    .catch(() => { hoverDetail.delete(id) })
}

function showHover(card, base) {
  // Списочные поля дополняем деталью, если она уже пришла. Пока не пришла —
  // панель показывается сразу с названием и автором, а факты дорисуются.
  const w = Object.assign({}, base, hoverDetail.get(base && base.id) || {})
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


// Дочитана ли книга. Плоский порог 0.98 годился для коротких книг и врал на
// длинных: у фанфика в 197 глав это ещё две непрочитанные главы, а карточка уже
// рисовала 100% и зелёную галочку. Поэтому меряем не долей, а тем, сколько
// осталось: меньше одной главы — дочитано. Для книг без счётчика глав остаётся
// прежний порог.
function isFinished(ratio, chaptersCount) {
  const chapters = Number(chaptersCount) || 0
  if (chapters >= 1) return (1 - (ratio || 0)) * chapters < 1
  return (ratio || 0) >= 0.98
}

function bookCard(w, ratio, hasUpdate) {
  const card = document.createElement('div')
  const readState = isFinished(ratio, w.chapters_count) ? 'read' : ratio > 0 ? 'partial' : 'unread'
  // Дочитано (read) — полоса всегда 100%. foliate почти никогда не даёт ровно
  // 1.0 на последней странице (типично 0.9999…), поэтому «дочитано» — это не
  // строгое ratio>=1, иначе дочитанная книга застревала на визуальном максимуме
  // недочитанной. Насколько «почти» — считает isFinished по объёму книги.
  const done = readState === 'read'
  // Плашку «обновление» не показываем на дочитанной книге: ficbook-лента метит
  // has_update при любой активности автора (не только новые главы), и дочитанная
  // книга висела с ложной плашкой. Когда реально докачается новая глава,
  // непрочитанный контент уронит ratio ниже порога — плашка вернётся сама.
  const showUpdate = hasUpdate && !done
  // Без сети книга без офлайн-копии открыться не сможет — помечаем, чтобы тап
  // по ней не был тупиком (CSS гасит такие карточки только в режиме офлайна).
  const noCopy = !isOffline(w.id) ? 'no-offline-copy' : ''
  card.className = ['book-card', readState, showUpdate ? 'has-update' : '', noCopy]
    .filter(Boolean).join(' ')
  const pct = Math.round((ratio || 0) * 100)
  // Всегда запрашиваем /cover: если обложки нет, бэкенд лениво сгенерирует её
  // ИИ и вернёт картинку. Пока грузится/если не вышло — виден текстовый фолбэк.
  // Просим превью под размер ячейки, а не оригинал: обложки весят в среднем
  // 169 КБ (максимум 3.5 МБ) и составляли 95.7% веса страницы библиотеки, хотя
  // рисуются в ~160×240 px. Сервер отдаёт кэшированный WEBP (?w=320|640),
  // оригинал остаётся доступен без параметра — он нужен на странице книги.
  const _cov = `/api/reader/${w.id}/cover?v=${w.cover_v||0}`
  const cover = coverImg(
    `${_cov}&w=320`, w.title,
    `${_cov}&w=320 320w, ${_cov}&w=640 640w`,
    '(max-width: 700px) 160px, 240px',
  )
  const badge = showUpdate ? '<span class="upd-badge" title="Есть новые главы">обновление</span>' : ''
  const offBadge = isOffline(w.id) ? '<span class="offline-badge" title="Доступна офлайн">офлайн</span>' : ''
  // Явное состояние чтения: ✓ у дочитанной, процент у начатой — 5px-полосы
  // прогресса не хватало, «не видно, что книга прочитана/начата».
  const readBadge = done ? '<span class="read-badge" title="Прочитано">✓</span>' : ''
  const pctBadge = readState === 'partial' ? `<span class="pct-badge">${pct}%</span>` : ''
  card.innerHTML = `
    <div class="book-cover">${cover}${badge}${offBadge}${readBadge}${pctBadge}<button class="book-del-btn" title="Удалить книгу" aria-label="Удалить">✕</button><button class="book-read-btn" title="Читать" aria-label="Читать ${escapeHtml(w.title || 'книгу')}"><span aria-hidden="true">▶</span> Читать</button></div>
    <div class="book-meta">
      <div class="b-title">${escapeHtml(w.title || 'Без названия')}</div>
      <div class="b-author${w.author ? ' b-link' : ''}" data-flt="author">${escapeHtml(w.author || '')}</div>
      ${w.series ? `<div class="b-series b-link" data-flt="series" title="Показать всю серию">📚 ${escapeHtml(w.series)}${w.series_index ? ' #' + w.series_index : ''}</div>` : ''}
    </div>
    <div class="book-progress"><i style="width:${done ? 100 : (ratio > 0 ? Math.min(pct, 99) : 0)}%"></i></div>`
  // Доступность: карточка — это кнопка «открыть книгу». Делаем её достижимой с
  // клавиатуры (Tab) и активируемой Enter/Space (focus-visible уже стилизован).
  card.tabIndex = 0
  card.setAttribute('role', 'button')
  card.setAttribute('aria-label',
    `${w.title || 'Книга'}${w.author ? ', ' + w.author : ''}`)
  // Данные книги держим на самом элементе: обработчики висят на гриде и
  // достают их отсюда, не создавая замыкание на каждую карточку.
  card._w = w
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
  card._cal = b
  return card
}

// ===================== Делегирование событий грида =====================
// Один слушатель на весь список вместо шести на каждую карточку: на полутора
// тысячах книг это была половина времени отрисовки и тысячи живых обработчиков.

async function removeCard(card, w) {
  if (!confirm(`Удалить «${w.title || 'книгу'}»?`)) return
  card.style.opacity = '0.4'; card.style.pointerEvents = 'none'
  try {
    const r = await fetch(`/api/library/${w.id}`, { method: 'DELETE' })
    if (r.ok) card.remove()
    else { card.style.opacity = ''; card.style.pointerEvents = ''; alert('Ошибка удаления') }
  } catch { card.style.opacity = ''; card.style.pointerEvents = '' }
}

async function importCalibre(card, b) {
  card.style.opacity = '0.5'; card.style.pointerEvents = 'none'
  try {
    const work = await api.post(`/api/calibre/import/${b.calibre_id}`, {})
    await loadLibrary()
    openReader(work)
  } catch (err) {
    card.style.opacity = ''; card.style.pointerEvents = ''
    alert('Не удалось открыть книгу из Calibre: ' + err.message)
  }
}

const bookGrid = $('#book-grid')

bookGrid.addEventListener('click', (e) => {
  const card = e.target.closest('.book-card')
  if (!card) return
  const w = card._w
  if (w && e.target.closest('.book-read-btn')) { e.stopPropagation(); hideHoverNow(); openReader(w); return }
  if (w && e.target.closest('.book-del-btn')) { e.stopPropagation(); removeCard(card, w); return }
  const flt = w && e.target.closest('[data-flt]')
  if (flt) {
    const val = flt.dataset.flt === 'series' ? w.series : w.author
    if (val) { e.stopPropagation(); hideHoverNow(); filterBy(val); return }
  }
  if (w) { hideHoverNow(); openBookPage(w); return }
  if (card._cal) importCalibre(card, card._cal)
})

bookGrid.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return
  // Только когда фокус на самой карточке: активацию кнопки удаления внутри
  // неё перехватывать нельзя.
  const card = e.target
  if (!card.classList || !card.classList.contains('book-card')) return
  e.preventDefault()
  if (card._w) { hideHoverNow(); openBookPage(card._w) }
  else if (card._cal) importCalibre(card, card._cal)
})

// mouseenter/mouseleave не всплывают — делегируем через mouseover/mouseout,
// отсеивая переходы между потрохами одной и той же карточки.
let hoverCard = null
let hoverEnterT = null

bookGrid.addEventListener('mouseover', (e) => {
  if (!CAN_HOVER) return
  const card = e.target.closest('.book-card')
  if (!card || card === hoverCard) return
  hoverCard = card
  hideHoverNow()                       // мгновенно убрать панель прошлой карточки
  clearTimeout(hoverEnterT)
  if (card._w) {
    // Запрос уходит сразу, а панель ждёт свои 350 мс — за это время деталь
    // обычно успевает прийти, и панель открывается уже полной.
    prefetchDetail(card._w.id)
    hoverEnterT = setTimeout(() => showHover(card, card._w), 350)
  }
})

bookGrid.addEventListener('mouseout', (e) => {
  if (!CAN_HOVER) return
  const card = e.target.closest('.book-card')
  if (!card || card !== hoverCard) return
  const to = e.relatedTarget
  if (to && to.closest && to.closest('.book-card') === card) return   // всё ещё внутри
  hoverCard = null
  clearTimeout(hoverEnterT)
  hideHover()
})

// Общий статус-строки внизу формы добавления: показать сообщение (или ошибку).
function setIngestStatus(msg, { error = false } = {}) {
  const status = $('#ingest-status')
  status.hidden = false
  status.classList.toggle('error', error)
  status.textContent = msg
}

// Тело ошибки приходит как {"detail": "…"} — показывать пользователю сырой JSON
// незачем, ему адресован только текст.
function errText(err) {
  const d = err && err.data && err.data.detail
  const s = d ? String(d) : ((err && err.message) || String(err))
  return s.length > 300 ? s.slice(0, 300) + '…' : s
}

// Запрос, по которому мы уже ответили «это у тебя есть». Повторный Enter по
// нему означает «всё равно скачай»: иначе книгу, чьё название совпадает с
// уже имеющейся (соседний том, тёзка), нельзя было бы добавить вообще.
let dupQuery = ''

// Единый вход: ввод фильтрует библиотеку, Enter решает — показать своё или качать.
// URL → адаптеры/FanFicFare, название → searchfloor/readli.
$('#ingest-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const input = $('#lib-q')
  const q = input.value.trim()
  if (!q) return
  // Несколько ссылок разом — это не «скачай фанфик», а «собери из них книгу».
  const many = q.split(/\s+/).filter((u) => /^https?:\/\//i.test(u))
  if (many.length > 1) {
    openWebModal(many)
    input.value = ''
    applyFilterNow('')
    return
  }
  const isUrl = isUrlQuery(q)
  applyFilterNow(isUrl ? '' : q)
  // Книга уже своя — показываем её, а не качаем повторно. Список под полем уже
  // отфильтрован тем же запросом, поэтому «показать» = оставить как есть;
  // единственное совпадение открываем сразу.
  if (!isUrl && q !== dupQuery) {
    const { works, calibre } = findLibMatches(q)
    if (works.length === 1 && !calibre.length) {
      setIngestStatus('Уже в библиотеке — открываю: ' + works[0].title)
      openBookPage(works[0])
      return
    }
    if (works.length || calibre.length) {
      dupQuery = q
      const parts = []
      if (works.length) parts.push(`в библиотеке: ${works.length}`)
      if (calibre.length) parts.push(`в Calibre: ${calibre.length}`)
      setIngestStatus(
        `Уже есть (${parts.join(', ')}) — показано ниже. `
        + 'Уточните запрос, чтобы открыть книгу, или нажмите Enter ещё раз — скачаю.'
      )
      return
    }
  }
  setIngestStatus(isUrl ? 'Скачиваю…' : 'Ищу по названию…')
  try {
    const work = await api.post('/api/ingest', { query: q })
    setIngestStatus('Готово: ' + (work.title || 'книга добавлена'))
    dupQuery = ''
    // Поле не чистим: это фильтр, и по нему скачанная книга сразу видна в списке
    // (loadLibrary применит текущее значение поля сам). Для ссылки фильтровать
    // нечем — там поле освобождаем.
    if (isUrl) input.value = ''
    await loadLibrary()
  } catch (err) {
    setIngestStatus('Не удалось добавить: ' + errText(err), { error: true })
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

$('#lib-q').addEventListener('input', (e) => {
  const v = e.target.value.trim()
  // Пауза в 120 мс: при быстром наборе грид перерисовывается один раз, а не
  // на каждое нажатие (полный список — ~300 мс на перерисовку).
  clearTimeout(filterTimer)
  filterTimer = setTimeout(() => applyFilterNow(isUrlQuery(v) ? '' : v), 120)
  // Прошлый вердикт относился к прошлому запросу — иначе поверх нового ввода
  // висит красная ошибка от предыдущей попытки.
  $('#ingest-status').hidden = true
  dupQuery = ''
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


// ===================== Книга из статей =====================
// Несколько ссылок → одна книга: ссылка = глава, картинки скачиваются внутрь.
// Сборка идёт в фоне на бэкенде (десятки страниц не укладываются в таймаут
// nginx), поэтому здесь только запуск и поллинг /api/ingest/web/status.
let webPolling = false

function setWebStatus(msg, { error = false } = {}) {
  const el = $('#web-status')
  el.hidden = false
  el.classList.toggle('error', error)
  el.textContent = msg
}

function webUrls() {
  return $('#web-urls').value.split(/\s+/).filter((u) => /^https?:\/\//i.test(u))
}

function updateWebCount() {
  const n = webUrls().length
  $('#web-count').textContent = n ? `ссылок: ${n}` : ''
}

export function openWebModal(urls) {
  if (urls && urls.length) $('#web-urls').value = urls.join('\n')
  $('#web-overlay').hidden = false
  updateWebCount()
  $('#web-urls').focus()
}

$('#web-btn')?.addEventListener('click', () => openWebModal())
$('#web-close')?.addEventListener('click', () => { $('#web-overlay').hidden = true })
$('#web-overlay')?.addEventListener('click', (e) => {
  if (e.target === $('#web-overlay')) $('#web-overlay').hidden = true
})
$('#web-urls')?.addEventListener('input', updateWebCount)

// Опрос фоновой задачи: onDone получает result, ошибки показываем сами.
async function pollWebJob(label, onDone) {
  if (webPolling) return
  webPolling = true
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const st = await api.get('/api/ingest/web/status')
      if (st.status === 'running') {
        const pr = st.progress || {}
        if (st.kind === 'build' && pr.total) {
          setWebStatus(`${label}: ${pr.current}/${pr.total} — ${(pr.url || '').slice(0, 70)}`)
        } else {
          setWebStatus(`${label}…`)
        }
        continue
      }
      if (st.status === 'error') { setWebStatus('Не вышло: ' + st.error, { error: true }); return }
      if (st.status === 'done') { await onDone(st.result || {}); return }
      setWebStatus('Задача не запустилась', { error: true })
      return
    }
  } catch (err) {
    setWebStatus('Ошибка связи: ' + err.message.slice(0, 160), { error: true })
  } finally {
    webPolling = false
  }
}

$('#web-discover')?.addEventListener('click', async () => {
  const urls = webUrls()
  if (!urls.length) { setWebStatus('Вставьте хотя бы одну ссылку', { error: true }); return }
  setWebStatus('Ищу остальные части серии…')
  try {
    await api.post('/api/ingest/web/discover', { url: urls[0] })
  } catch (err) {
    setWebStatus('Не вышло: ' + err.message.slice(0, 160), { error: true }); return
  }
  await pollWebJob('Ищу части', async (res) => {
    const parts = res.parts || []
    if (parts.length) {
      // найденное объединяем с уже вставленным вручную (продолжение серии
      // нередко живёт на другом сайте — его ссылки пользователь добавил сам)
      const known = new Set(parts.map((p) => p.url))
      const extra = webUrls().filter((u) => !known.has(u) && !known.has(u + '/'))
      $('#web-urls').value = parts.map((p) => p.url).concat(extra).join('\n')
      updateWebCount()
      if (!$('#web-title').value.trim() && res.series) $('#web-title').value = res.series
    }
    setWebStatus(`Нашлось частей: ${parts.length}. ${res.note || ''}`)
  })
})

$('#web-build')?.addEventListener('click', async () => {
  const urls = webUrls()
  if (!urls.length) { setWebStatus('Вставьте хотя бы одну ссылку', { error: true }); return }
  setWebStatus(`Собираю книгу из ${urls.length} статей…`)
  try {
    await api.post('/api/ingest/web', {
      urls,
      title: $('#web-title').value.trim(),
      author: $('#web-author').value.trim(),
    })
  } catch (err) {
    setWebStatus('Не вышло: ' + err.message.slice(0, 160), { error: true }); return
  }
  await pollWebJob('Скачиваю', async (res) => {
    let msg = `Готово: «${res.title}» — глав ${res.chapters}, картинок ${res.images}`
    if (res.images_skipped) msg += ` (пропущено картинок: ${res.images_skipped})`
    if ((res.warnings || []).length) msg += `; часть ссылок не открылась: ${res.warnings.length}`
    setWebStatus(msg)
    await loadLibrary()
  })
})
