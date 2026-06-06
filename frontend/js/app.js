// Фронтенд читалки: библиотека + reader на foliate-js, синхронизация прогресса.
import '/vendor/foliate-js/view.js'

const $ = (s) => document.querySelector(s)
const api = {
  async get(url) { const r = await fetch(url); if (!r.ok) throw new Error(await r.text()); return r.json() },
  async put(url, body) { const r = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw new Error(await r.text()); return r.json() },
  async post(url, body) { const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) throw new Error(await r.text()); return r.json() },
}

// ===================== Настройки вида (localStorage) =====================
const PREFS_KEY = 'reader.prefs'
const prefs = Object.assign(
  { theme: 'day', fontScale: 1, marginLevel: 1, fontFamily: 'merriweather', flow: 'paginated', columns: 1 },
  JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'),
)
if (prefs.fontFamily === 'serif') prefs.fontFamily = 'merriweather'
if (prefs.fontFamily === 'sans')  prefs.fontFamily = 'open-sans'
const savePrefs = () => localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))
const MARGIN_INLINE = { 0: 760, 1: 620, 2: 480 } // уровень полей → max-inline-size (меньше = шире поля)
const MARGIN_NAME = { 0: 'узк.', 1: 'сред.', 2: 'шир.' }
const FONT_STACKS = {
  'merriweather': '"Merriweather", Georgia, serif',
  'lora':         '"Lora", Georgia, serif',
  'pt-serif':     '"PT Serif", Georgia, serif',
  'georgia':      'Georgia, "Times New Roman", serif',
  'open-sans':    '"Open Sans", system-ui, sans-serif',
  'nunito':       '"Nunito", system-ui, sans-serif',
  'pt-sans':      '"PT Sans", system-ui, sans-serif',
}
const GFONTS = 'https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,700;1,400;1,700&family=Merriweather:ital,wght@0,400;0,700;1,400;1,700&family=Nunito:ital,wght@0,400;0,700;1,400;1,700&family=Open+Sans:ital,wght@0,400;0,700;1,400;1,700&family=PT+Serif:ital,wght@0,400;0,700;1,400;1,700&family=PT+Sans:ital,wght@0,400;0,700;1,400;1,700&display=swap'

document.documentElement.dataset.theme = prefs.theme

// ===================== БИБЛИОТЕКА =====================
let libWorks = [], libCalibre = [], libProgress = {}, libUpdated = new Set()

async function loadLibrary() {
  libWorks = await api.get('/api/library')
  const monitored = await api.get('/api/monitored').catch(() => [])
  libUpdated = new Set(monitored.filter((m) => m.has_update && m.work_id).map((m) => m.work_id))
  libProgress = {}
  for (const w of libWorks) {
    const prog = await api.get(`/api/progress/${w.id}`).catch(() => ({ ratio: 0 }))
    libProgress[w.id] = prog.ratio || 0
  }
  // Загружаем Calibre один раз (фоном, не блокируем рендер)
  api.get('/api/calibre/books').then(books => { libCalibre = books || [] }).catch(() => {})
  applyLibFilter('')
}

function applyLibFilter(q) {
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

function bookCard(w, ratio, hasUpdate) {
  const card = document.createElement('div')
  card.className = 'book-card'
  const pct = Math.round((ratio || 0) * 100)
  const fallback = `<span class="cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
  const cover = w.cover_path
    ? `<img src="/api/reader/${w.id}/cover?v=${w.updated_at||0}" alt="" onerror="this.remove()" />${fallback}`
    : fallback
  const badge = hasUpdate ? '<span class="upd-badge" title="Есть новые главы">обновление</span>' : ''
  card.innerHTML = `
    <div class="book-cover">${cover}${badge}<button class="book-del-btn" title="Удалить книгу" aria-label="Удалить">✕</button></div>
    <div class="book-meta">
      <div class="b-title">${escapeHtml(w.title || 'Без названия')}</div>
      <div class="b-author">${escapeHtml(w.author || '')}</div>
    </div>
    <div class="book-progress"><i style="width:${pct}%"></i></div>`
  card.addEventListener('click', () => openReader(w))
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

const escapeHtml = (s) => (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))

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

// ===================== Аккаунты и обновления =====================
const accStatus = (msg, err) => {
  const el = $('#accounts-status'); el.hidden = false
  el.classList.toggle('error', !!err); el.textContent = msg
}
async function loadAccounts() {
  const accs = await api.get('/api/accounts').catch(() => [])
  const box = $('#accounts-list'); box.innerHTML = ''
  if (!accs.length) box.innerHTML = '<div class="acc-row">Аккаунтов нет</div>'
  for (const a of accs) {
    const row = document.createElement('div'); row.className = 'acc-row'
    row.innerHTML = `<span>${escapeHtml(a.site)} — ${escapeHtml(a.username)}</span>`
    const del = document.createElement('button'); del.className = 'icon-btn'; del.textContent = '✕'
    del.addEventListener('click', async () => { await fetch(`/api/accounts/${a.id}`, { method: 'DELETE' }); loadAccounts() })
    row.append(del); box.append(row)
  }
}
async function loadMonitored() {
  const items = await api.get('/api/monitored').catch(() => [])
  const box = $('#monitored-list'); box.innerHTML = ''
  if (!items.length) { box.innerHTML = '<div class="mon-row">Пока ничего не отслеживается</div>'; return }
  for (const m of items) {
    const row = document.createElement('div'); row.className = 'mon-row' + (m.has_update ? ' has-update' : '')
    const name = m.title || m.source_url
    row.innerHTML = `<span class="mon-title">${escapeHtml(name)}</span>` +
      `<span>${m.last_seen_chapters} гл.${m.has_update ? ' <span class="badge">обновление</span>' : ''}</span>`
    box.append(row)
  }
}
$('#accounts-btn').addEventListener('click', () => {
  $('#accounts-overlay').hidden = false; loadAccounts(); loadMonitored()
})
$('#accounts-close').addEventListener('click', () => { $('#accounts-overlay').hidden = true })
$('#accounts-overlay').addEventListener('click', (e) => { if (e.target.id === 'accounts-overlay') $('#accounts-overlay').hidden = true })
$('#account-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const site = $('#acc-site').value, username = $('#acc-user').value.trim(), password = $('#acc-pass').value
  if (!username || !password) { accStatus('Введите логин и пароль', true); return }
  try {
    await api.post('/api/accounts', { site, username, password })
    $('#acc-user').value = ''; $('#acc-pass').value = ''
    accStatus('Аккаунт сохранён'); loadAccounts()
  } catch (err) { accStatus('Ошибка: ' + err.message.slice(0, 120), true) }
})
async function checkUpdates(statusFn) {
  statusFn('Проверяю обновления…')
  try {
    const r = await api.post('/api/monitored/check', {})
    statusFn(`Проверено: ${r.checked}, с обновлениями: ${r.with_updates}, докачано: ${r.downloaded}`)
    loadMonitored(); loadLibrary()
  } catch (err) { statusFn('Ошибка: ' + err.message.slice(0, 140), true) }
}
$('#check-updates').addEventListener('click', () => checkUpdates(accStatus))
// Кнопка на главной: статус показываем в строке ingest-status.
$('#check-updates-main').addEventListener('click', () => checkUpdates((msg, err) => {
  const el = $('#ingest-status'); el.hidden = false; el.classList.toggle('error', !!err); el.textContent = msg
}))

// ===================== ЧИТАЛКА =====================
let view = null
let currentWork = null
let saveTimer = null

async function openReader(work) {
  ttsStop()
  currentWork = work
  $('#library').hidden = true
  $('#reader').hidden = false
  $('#reader-title').textContent = work.title || ''

  // Очистить прошлый экземпляр.
  $('#view-host').innerHTML = ''
  view = document.createElement('foliate-view')
  $('#view-host').append(view)

  // Загружаем файл как Blob → File с корректным именем (для детекта FB2).
  const resp = await fetch(`/api/reader/${work.id}/file`)
  const blob = await resp.blob()
  const name = `book.${work.file_format || 'epub'}`
  const file = new File([blob], name, { type: blob.type })

  await view.open(file)
  view.addEventListener('relocate', onRelocate)
  view.addEventListener('load', attachKeysToDoc)
  applyViewStyles()
  buildTOC()

  // Восстановить позицию: точный CFI, иначе ratio (напр. импорт из ReadEra), иначе начало.
  const prog = await api.get(`/api/progress/${work.id}`).catch(() => null)
  if (prog && prog.locator) {
    await view.init({ lastLocation: prog.locator })
  } else {
    await view.init({ showTextStart: true })
    if (prog && prog.ratio > 0) { try { await view.goToFraction(prog.ratio) } catch {} }
  }
}

function onRelocate(e) {
  const { fraction, cfi } = e.detail
  const pct = Math.round((fraction || 0) * 100)
  $('#progress-slider').value = fraction || 0
  $('#progress-label').textContent = pct + '%'
  // Дебаунс-сохранение прогресса на сервер.
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    if (!currentWork) return
    api.put(`/api/progress/${currentWork.id}`, { ratio: fraction || 0, locator: cfi || '' }).catch(() => {})
  }, 900)
  if (ttsSt.advance) { ttsSt.advance = false; setTimeout(() => { if (ttsSt.active) ttsReadPage() }, 350) }
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
  const sidePad = { 0: 4, 1: 8, 2: 14 }[prefs.marginLevel] ?? 8
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
    p, li, blockquote, dd { line-height: 1.55; text-align: justify; hyphens: auto; }
    img { max-width: 100%; height: auto; }
    .tts-reading { background: ${accent}28 !important; outline: 2px solid ${accent}88; outline-offset: 3px; border-radius: 3px; }
    ::highlight(tts-word) { background-color: ${accent}; color: #fff; border-radius: 2px; }
  `
}
function applyViewStyles() {
  if (!view || !view.renderer) return
  const r = view.renderer
  // Сначала раскладка (flow/колонки), потом стили: render() триггерится атрибутами,
  // и к моменту его вызова наш bookCSS уже не перетирается лишним «paginated-кадром».
  if (prefs.flow === 'scrolled') {
    // Лента: одна колонка во всю ширину области (конкретный px, не «бесконечность»).
    const w = Math.max(600, ($('#view-host')?.clientWidth || 1000))
    r.setAttribute('max-column-count', '1')
    r.setAttribute('max-inline-size', String(w))
  } else {
    // Страницы: 1 или 2 колонки по выбору; ширина колонки — от уровня полей.
    r.setAttribute('max-column-count', String(prefs.columns || 1))
    r.setAttribute('max-inline-size', String(MARGIN_INLINE[prefs.marginLevel]))
  }
  r.setAttribute('gap', '6%')
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

// ===================== Навигация и панели =====================
$('#back-btn').addEventListener('click', () => {
  ttsStop()
  $('#reader').hidden = true
  $('#library').hidden = false
  $('#search-results').innerHTML = ''; $('#search-meta').textContent = ''; $('#search-input').value = ''
  currentWork = null; view = null
  loadLibrary()
})
$('#prev-btn').addEventListener('click', () => view?.prev())
$('#next-btn').addEventListener('click', () => view?.next())
$('#progress-slider').addEventListener('input', (e) => view?.goToFraction(parseFloat(e.target.value)))

// Зоны клика по краям — перелистывание.
$('#tap-prev').addEventListener('click', () => view?.prev())
$('#tap-next').addEventListener('click', () => view?.next())

// Клавиатура: стрелки, PageUp/Down, Home/End, пробел (вниз; Shift+пробел — вверх).
function handleKey(e) {
  if ($('#reader').hidden || !view) return
  const k = e.key
  if (k === 'ArrowLeft' || k === 'PageUp') { view.prev(); e.preventDefault() }
  else if (k === 'ArrowRight' || k === 'PageDown') { view.next(); e.preventDefault() }
  else if (k === ' ' || k === 'Spacebar') { e.shiftKey ? view.prev() : view.next(); e.preventDefault() }
  else if (k === 'Home') { view.goToFraction(0); e.preventDefault() }
  else if (k === 'End') { view.goToFraction(1); e.preventDefault() }
}
document.addEventListener('keydown', handleKey)
// Когда фокус внутри книги (iframe), события клавиш ловим и там.
let wheelThrottle = false
function wheelNav(deltaY) {
  if (!view || prefs.flow === 'scrolled') return
  if (wheelThrottle) return
  wheelThrottle = true
  setTimeout(() => { wheelThrottle = false }, 400)
  if (deltaY > 0) view.next(); else view.prev()
}
let bookDoc = null
function attachKeysToDoc(e) {
  bookDoc = e.detail.doc
  try {
    e.detail.doc.addEventListener('keydown', handleKey)
    e.detail.doc.addEventListener('wheel', (ev) => {
      if (prefs.flow === 'scrolled') {
        if (ev.deltaY < 0 && (view?.renderer?.start || 0) <= 0) {
          ev.preventDefault(); view.prev(); return
        }
        if (ev.deltaY > 0) {
          const _p = view?.renderer
          const _iH = bookDoc?.defaultView?.innerHeight || 99999
          if (_p && (_p.start + _p.size) >= _iH - 150) {
            ev.preventDefault(); view.next(); return
          }
        }
        return
      }
      ev.preventDefault()
      wheelNav(ev.deltaY)
    }, { passive: false })
  } catch {}
}
$('#view-host').addEventListener('wheel', (e) => {
  if ($('#reader').hidden || !view || prefs.flow === 'scrolled') return
  e.preventDefault()
  wheelNav(e.deltaY)
}, { passive: false })

// Переприменять раскладку при изменении размера окна (особенно ширину «ленты»).
let resizeTimer = null
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => { if (!$('#reader').hidden && view) applyViewStyles() }, 200)
})

function openPanel(id) { closePanels(); $(id).hidden = false; $('#panel-overlay').hidden = false }
function closePanels() {
  $('#toc-panel').hidden = true; $('#settings-panel').hidden = true
  $('#search-panel').hidden = true; $('#panel-overlay').hidden = true
}
$('#toc-btn').addEventListener('click', () => openPanel('#toc-panel'))
$('#settings-btn').addEventListener('click', () => openPanel('#settings-panel'))
$('#search-btn').addEventListener('click', () => { openPanel('#search-panel'); $('#search-input').focus() })
$('#panel-overlay').addEventListener('click', closePanels)

// ===================== Поиск по книге =====================
// foliate view.search() — асинхронный генератор: по секциям выдаёт совпадения
// (cfi + excerpt {pre,match,post}) и прогресс; сам подсвечивает их в тексте.
let searchSeq = 0
$('#search-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  if (!view) return
  const q = $('#search-input').value.trim()
  const results = $('#search-results'); results.innerHTML = ''
  const meta = $('#search-meta')
  view.clearSearch?.()
  if (!q) { meta.textContent = ''; return }
  const seq = ++searchSeq // отменяем результаты прошлого запроса
  // OR-поиск: «слово1|слово2» → несколько последовательных запросов.
  const terms = q.split('|').map(t => t.trim()).filter(Boolean)
  const isMulti = terms.length > 1
  meta.textContent = isMulti ? `Поиск по ${terms.length} словам…` : 'Поиск…'
  let count = 0
  try {
    for (const term of terms) {
      if (seq !== searchSeq) return
      for await (const r of view.search({ query: term })) {
        if (seq !== searchSeq) return
        if (r === 'done') break
        if (r.subitems) {
          for (const sub of r.subitems) {
            count++
            const lbl = isMulti ? ((r.label ? r.label + ' ' : '') + '[' + term + ']') : r.label
            results.append(searchResult(lbl, sub))
          }
          meta.textContent = `Найдено: ${count}`
        } else if (typeof r.progress === 'number') {
          meta.textContent = isMulti
            ? `«${term}»: ${Math.round(r.progress * 100)}% (всего ${count})`
            : `Поиск… ${Math.round(r.progress * 100)}% (найдено ${count})`
        }
      }
    }
    if (seq === searchSeq) meta.textContent = count ? `Найдено совпадений: ${count}` : 'Ничего не найдено'
  } catch (err) {
    if (seq === searchSeq) meta.textContent = 'Ошибка поиска: ' + (err?.message || '')
  }
})

function searchResult(label, sub) {
  const ex = sub.excerpt || {}
  const a = document.createElement('a')
  a.className = 'search-result'; a.href = '#'
  a.innerHTML =
    (label ? `<span class="sr-label">${escapeHtml(label)}</span>` : '') +
    `<span class="sr-ex">${escapeHtml(ex.pre || '')}<mark>${escapeHtml(ex.match || '')}</mark>${escapeHtml(ex.post || '')}</span>`
  a.addEventListener('click', (ev) => { ev.preventDefault(); view.goTo(sub.cfi); closePanels() })
  return a
}


// ===================== TTS =====================
const ttsSt = { active: false, paused: false, chunks: [], idx: 0, rate: 1, voiceId: 'xenia', voiceLang: 'ru-RU', advance: false, currentEl: null, audio: null, rafId: null, wordIdx: 0, wordTimings: [], allVoices: [], _chunkBodyOffset: 0, prefetch: {} }

// Паттерн «визуального шума» — строки, которые TTS не должен произносить
const TTS_SKIP = /^[\s*\-~=_|•·×✦◦∗#—]{2,}$|^(\*\s+){2,}\*?$|^(-\s+){2,}-?$/

function ttsCleanLine(s) {
  if (TTS_SKIP.test(s)) return ''
  if (s.length <= 6 && /^[\d\s.,;:!?()/\\\[\]]+$/.test(s)) return ''
  s = s.replace(/https?:\/\/\S+/gi, '').trim()
  if (!s || s.length < 2) return ''
  return s
}

function ttsSplit(text) {
  const chunks = []
  for (const raw of text.split(/\n+/)) {
    const s = ttsCleanLine(raw.trim())
    if (!s || s.length < 2) continue
    if (s.length <= 3800) { chunks.push(s); continue }
    const sents = s.match(/[^.!?…]+[.!?…»]+\s*/g) || [s]
    for (const sent of sents) { const t = sent.trim(); if (t.length > 1) chunks.push(t) }
  }
  return chunks
}

// Заголовки, после которых начинается секция примечаний/сносок
const TTS_NOTES_RE = /^(примечани[яе]|сноск[аи]|footnotes?|notes?|переводчик|перевод\s*:)\s*:?\s*$/i

function ttsExtract() {
  if (!bookDoc) return []
  let blocks = [...bookDoc.querySelectorAll('p, h1, h2, h3, h4, h5, li, blockquote')]

  // Fallback: EPUB с текстом прямо в body (без <p>) — используем body.innerText
  const _iH = bookDoc.defaultView?.innerHeight || 0
  if (blocks.length < 5 && _iH > 2000) {
    try {
      const rawText = (bookDoc.body?.innerText || '').trim()
      if (rawText.length > 200) {
        const lines = rawText.split(/\n/).map(l => l.trim()).filter(l => l.length >= 5)
        const notesLine = lines.findIndex(l => TTS_NOTES_RE.test(l))
        const storyLines = notesLine > 0 ? lines.slice(0, notesLine) : lines
        if (storyLines.length > 0) {
          const pager = view?.renderer
          const pct = pager ? Math.max(0, pager.start / Math.max(_iH - (pager.size||800), 1)) : 0
          const startLine = Math.floor(pct * storyLines.length)
          return ttsSplit(storyLines.slice(Math.max(0, startLine - 1)).join('\n'))
        }
      }
    } catch {}
  }

  if (!blocks.length) return ttsSplit(bookDoc.body?.innerText || '')

  const notesIdx = blocks.findIndex(el => TTS_NOTES_RE.test(el.innerText?.trim() || ''))
  if (notesIdx > 0) blocks = blocks.slice(0, notesIdx)

  let startIdx = 0
  let startFound = false

  // 1. Старт с выделения
  try {
    const sel = bookDoc.getSelection()
    if (sel && !sel.isCollapsed) {
      let el = sel.anchorNode?.nodeType === Node.TEXT_NODE ? sel.anchorNode.parentElement : sel.anchorNode
      while (el && !['P','H1','H2','H3','H4','H5','LI','BLOCKQUOTE'].includes(el.tagName || '')) el = el.parentElement
      const i = el ? blocks.indexOf(el) : -1
      if (i >= 0) { startIdx = i; startFound = true }
    }
  } catch {}

  // 2. Определяем первый видимый блок
  if (!startFound) {
    try {
      const pager = view?.renderer
      const pgStart = pager?.start ?? 0
      const pgSize  = pager?.size  ?? 800
      if (pager?.scrolled) {
        // SCROLLED: iframe = вся глава (высота 30000-50000px), docScrollY=0
        // pager.start = #container.scrollTop = document-координата видимой области
        // elementFromPoint(x, y) принимает y в document-пространстве (не viewport!)
        // поэтому y = pager.start + смещение
        outer: for (let row = 0; row < 6; row++) {
          for (let col = 0; col < 5; col++) {
            const x = 20 + col * 80
            const y = pgStart + 15 + row * 30
            let node = bookDoc.elementFromPoint(x, y)
            while (node && node.tagName !== 'HTML' && node.tagName !== 'BODY') {
              const i = blocks.indexOf(node)
              if (i >= 0) { startIdx = i; startFound = true; break outer }
              node = node.parentElement
            }
          }
        }
        // Запасной вариант: первый блок, чья нижняя граница >= pgStart
        if (!startFound) {
          const i = blocks.findIndex(el => (el.offsetTop || 0) + (el.offsetHeight || 30) > pgStart)
          if (i >= 0) startIdx = i
        }
      } else {
        // PAGINATED: iframe горизонтально расширен, x = pager.start + offset
        outer: for (let row = 0; row < 5; row++) {
          for (let col = 0; col < 5; col++) {
            const x = pgStart + 20 + col * Math.round(pgSize * 0.18)
            const y = 15 + row * 30
            let node = bookDoc.elementFromPoint(x, y)
            while (node && node.tagName !== 'HTML' && node.tagName !== 'BODY') {
              const i = blocks.indexOf(node)
              if (i >= 0) { startIdx = i; break outer }
              node = node.parentElement
            }
          }
        }
      }
    } catch {}
  }

  // 3. Фильтр: только блоки видимой области
  let src
  try {
    const pager = view?.renderer
    if (pager) {
      const pgStart = pager.start ?? 0
      const pgEnd   = pgStart + (pager.size ?? 800)
      const pageBlocks = blocks.slice(startIdx).filter(el => {
        try {
          if (pager.scrolled) {
            // scrolled: offsetTop = document y = те же координаты что pager.start
            const top = el.offsetTop || 0
            return top + (el.offsetHeight || 30) > pgStart + 2 && top < pgEnd - 2
          } else {
            const r = el.getBoundingClientRect()
            return r.width > 0 && r.height > 0 && r.right > pgStart + 2 && r.left < pgEnd - 2
          }
        } catch { return false }
      })
      src = pageBlocks.length > 0 ? pageBlocks : blocks.slice(startIdx, startIdx + 8)
    }
  } catch {}
  if (!src) src = blocks.slice(startIdx)
  const extracted = src.map(el => el.innerText?.trim()).filter(Boolean).join('\n')

  // Если текст подозрительно короткий, а body содержит намного больше — fallback на body
  try {
    const bodyText = (bookDoc.body?.innerText || '').trim()
    if (extracted.length < 300 && bodyText.length > extracted.length + 500) {
      const lines = bodyText.split(/\n/).map(l => l.trim()).filter(l => l.length >= 5)
      const notesLine = lines.findIndex(l => TTS_NOTES_RE.test(l))
      const storyLines = notesLine > 0 ? lines.slice(0, notesLine) : lines
      if (storyLines.join('').length > extracted.length + 200) {
        const pager = view?.renderer
        const iH2 = bookDoc.defaultView?.innerHeight || 0
        const pct2 = (pager && iH2 > 0) ? Math.max(0, pager.start / Math.max(iH2 - (pager.size||800), 1)) : 0
        const sl = Math.floor(pct2 * storyLines.length)
        return ttsSplit(storyLines.slice(Math.max(0, sl - 1)).join('\n'))
      }
    }
  } catch {}

  return ttsSplit(extracted)
}

// Вспомогательные: подсветка абзаца и слова
function findTextOffset(el, searchText) {
  if (!el || !bookDoc || !searchText) return -1
  try {
    const walker = bookDoc.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    let pos = 0, node
    while ((node = walker.nextNode())) {
      const idx = node.nodeValue.indexOf(searchText)
      if (idx >= 0) return pos + idx
      pos += node.nodeValue.length
    }
  } catch {}
  return -1
}
function ttsFindEl(text) { return null } // legacy stub
function ttsClearHighlights() {
  if (!bookDoc) return
  try { bookDoc.querySelectorAll('.tts-reading').forEach(el => el.classList.remove('tts-reading')) } catch {}
  try { bookDoc.defaultView?.CSS?.highlights?.delete('tts-word') } catch {}
}
function findCharRange(root, start, len) {
  if (!bookDoc || !root) return null
  const walker = bookDoc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let pos = 0, sNode, sOff, eNode, eOff, node
  while ((node = walker.nextNode())) {
    const nl = node.length
    if (!sNode && pos + nl > start) { sNode = node; sOff = start - pos }
    if (sNode && pos + nl >= start + len) { eNode = node; eOff = start + len - pos; break }
    pos += nl
  }
  if (!sNode) return null
  try {
    const r = bookDoc.createRange()
    r.setStart(sNode, Math.min(sOff, sNode.length))
    r.setEnd(eNode || sNode, Math.min(eOff ?? sOff + len, (eNode || sNode).length))
    return r
  } catch { return null }
}

function ttsPrecomputeWords(text, words) {
  let pos = 0
  const result = []
  for (const w of words || []) {
    if (!w.text) continue
    const idx = text.indexOf(w.text, pos)
    if (idx >= 0) { result.push({ t: w.t, charIndex: idx, charLength: w.text.length }); pos = idx + w.text.length }
  }
  return result
}

function ttsWordRaf() {
  if (!ttsSt.active || ttsSt.paused || !ttsSt.audio || !bookDoc) return
  const ms = ttsSt.audio.currentTime * 1000
  while (ttsSt.wordIdx < ttsSt.wordTimings.length && ttsSt.wordTimings[ttsSt.wordIdx].t <= ms) {
    const wt = ttsSt.wordTimings[ttsSt.wordIdx++]
    try {
      const range = findCharRange(bookDoc.body, ttsSt._chunkBodyOffset + wt.charIndex, wt.charLength)
      if (range) {
        const H = bookDoc.defaultView?.Highlight
        const hs = bookDoc.defaultView?.CSS?.highlights
        if (H && hs) hs.set('tts-word', new H(range))
        // Auto-scroll: keep highlighted word in view (scrolled mode only)
        try {
          const pager = view?.renderer
          if (pager?.scrolled) {
            const rect = range.getBoundingClientRect()
            const viewH = pager.size || bookDoc.defaultView?.innerHeight || 800
            if (rect.bottom > viewH * 0.82 || rect.top < 0) {
              const container = view.renderer.shadowRoot?.querySelector('#container')
              if (container) container.scrollBy({ top: rect.top - viewH * 0.25, behavior: 'smooth' })
            }
          }
        } catch {}
      }
    } catch {}
  }
  if (ttsSt.wordIdx < ttsSt.wordTimings.length) ttsSt.rafId = requestAnimationFrame(ttsWordRaf)
}

async function ttsSpeakChunk() {
  if (!ttsSt.active || ttsSt.idx >= ttsSt.chunks.length) {
    if (ttsSt.active) { ttsClearHighlights(); ttsSt.advance = true; view?.next() }
    return
  }
  const text = ttsSt.chunks[ttsSt.idx]
  ttsClearHighlights()
  ttsSt.wordIdx = 0; ttsSt.wordTimings = []

  // Позиция чанка в body для подсветки слов
  ttsSt._chunkBodyOffset = 0
  if (bookDoc?.body) {
    const s = text.trim().substring(0, 30)
    if (s) { const off = findTextOffset(bookDoc.body, s); if (off >= 0) ttsSt._chunkBodyOffset = off }
  }

  const total = ttsSt.chunks.length
  if (total) $('#tts-info').textContent = `${ttsSt.idx + 1} / ${total} (${Math.round((ttsSt.idx + 1) / total * 100)}%)`

  // Автодетект языка: Кириллица → русский голос, Latin → английский
  const _cyr = (text.match(/[\u0400-\u04FF]/g) || []).length
  const _lat = (text.match(/[a-zA-Z]/g) || []).length
  let voiceId = ttsSt.voiceId
  if (_cyr > _lat * 0.5 + 2 && !ttsSt.voiceLang.startsWith('ru'))
    voiceId = (ttsSt.allVoices.find(v => v.lang.startsWith('ru')) || {}).id || voiceId
  else if (_lat > _cyr * 0.5 + 5 && !ttsSt.voiceLang.startsWith('en'))
    voiceId = (ttsSt.allVoices.find(v => v.lang.startsWith('en')) || {}).id || voiceId

  const rateNum = Math.round((ttsSt.rate - 1) * 100)
  const rateStr = (rateNum >= 0 ? '+' : '') + rateNum + '%'

  // Prefetch: ждём если уже есть, иначе запрашиваем
  let audioUrl, wordTimingsReady
  if (ttsSt.prefetch[ttsSt.idx] instanceof Promise) {
    const cached = await ttsSt.prefetch[ttsSt.idx].catch(() => null)
    delete ttsSt.prefetch[ttsSt.idx]
    if (cached && ttsSt.active) { audioUrl = cached.audioUrl; wordTimingsReady = cached.wordTimings }
  }
  if (!audioUrl) {
    try {
      const resp = await fetch('/api/tts/synth', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: voiceId, rate: rateStr })
      })
      if (!resp.ok) throw new Error(await resp.text())
      const d = await resp.json()
      audioUrl = d.audio_url
      wordTimingsReady = ttsPrecomputeWords(text, d.words)
    } catch (e) {
      console.error('TTS synth:', e)
      if (ttsSt.active) { ttsSt.idx++; ttsSpeakChunk() }
      return
    }
  }
  if (!ttsSt.active) return

  ttsSt.wordTimings = wordTimingsReady || []
  ttsSt.wordIdx = 0

  // Prefetch следующего чанка пока играет текущий
  const _nextIdx = ttsSt.idx + 1
  if (ttsSt.active && _nextIdx < ttsSt.chunks.length && !ttsSt.prefetch[_nextIdx]) {
    const _nText = ttsSt.chunks[_nextIdx]
    const _nCyr = (_nText.match(/[\u0400-\u04FF]/g) || []).length
    const _nLat = (_nText.match(/[a-zA-Z]/g) || []).length
    let _nVoice = ttsSt.voiceId
    if (_nCyr > _nLat * 0.5 + 2 && !ttsSt.voiceLang.startsWith('ru'))
      _nVoice = (ttsSt.allVoices.find(v => v.lang.startsWith('ru')) || {}).id || _nVoice
    else if (_nLat > _nCyr * 0.5 + 5 && !ttsSt.voiceLang.startsWith('en'))
      _nVoice = (ttsSt.allVoices.find(v => v.lang.startsWith('en')) || {}).id || _nVoice
    ttsSt.prefetch[_nextIdx] = fetch('/api/tts/synth', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: _nText, voice: _nVoice, rate: rateStr })
    }).then(r => r.ok ? r.json() : null)
      .then(d => d ? { audioUrl: d.audio_url, wordTimings: ttsPrecomputeWords(_nText, d.words) } : null)
      .catch(() => null)
  }

  const audio = new Audio(audioUrl)
  ttsSt.audio = audio
  audio.addEventListener('ended', () => {
    if (ttsSt.audio !== audio) return
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
    if (ttsSt.active && !ttsSt.paused) { ttsSt.idx++; ttsSpeakChunk() }
  })
  audio.addEventListener('error', () => {
    if (ttsSt.audio !== audio) return
    if (ttsSt.active) { ttsSt.idx++; ttsSpeakChunk() }
  })
  try { await audio.play() } catch {}
  ttsSt.rafId = requestAnimationFrame(ttsWordRaf)
}

function ttsReadPage() {
  ttsSt.chunks = ttsExtract()
  ttsSt.idx = 0
  ttsSpeakChunk()
}

function ttsStart() {
  if (!view) return
  const _oldAudio = ttsSt.audio; ttsSt.audio = null
  if (_oldAudio) { _oldAudio.pause(); _oldAudio.src = '' }
  if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
  ttsSt.active = true; ttsSt.paused = false; ttsSt.advance = false
  // Возобновить с сохранённой позиции или начать с нуля
  if (ttsSt.chunks.length > 0 && ttsSt.idx < ttsSt.chunks.length) {
    ttsSpeakChunk()
  } else {
    ttsReadPage()
  }
  ttsUpdateUI()
}

function ttsStop() {
  ttsSt.active = false; ttsSt.paused = false; ttsSt.advance = false; ttsSt.prefetch = {}
  const _oldAudio = ttsSt.audio; ttsSt.audio = null
  if (_oldAudio) { _oldAudio.pause(); _oldAudio.src = '' }
  if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
  ttsClearHighlights()
  ttsUpdateUI()
}

function ttsPauseResume() {
  if (!ttsSt.active) { ttsStart(); return }
  if (!ttsSt.paused) {
    ttsSt.paused = true
    if (ttsSt.audio) ttsSt.audio.pause()
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
  } else {
    ttsSt.paused = false
    if (ttsSt.audio) { ttsSt.audio.play().catch(() => {}); ttsSt.rafId = requestAnimationFrame(ttsWordRaf) }
    else ttsSpeakChunk()
  }
  ttsUpdateUI()
}

function ttsUpdateUI() {
  const bar = $('#tts-bar'), btn = $('#tts-btn')
  if (!bar || !btn) return
  if (ttsSt.active) {
    bar.hidden = false
    $('#tts-play').textContent = ttsSt.paused ? '▶' : '⏸'
    btn.classList.add('tts-active')
  } else {
    bar.hidden = true
    $('#tts-info').textContent = ''
    btn.classList.remove('tts-active')
  }
}

async function ttsLoadVoices() {
  const sel = $('#tts-voice')
  if (!sel) return
  try {
    const data = await fetch('/api/tts/voices').then(r => r.json())
    const voices = data.voices || []
    ttsSt.allVoices = voices
    sel.innerHTML = ''
    const groups = {}
    for (const v of voices) {
      const grp = v.lang.startsWith('ru') ? 'Русский' : v.lang.startsWith('uk') ? 'Українська' : 'English'
      if (!groups[grp]) groups[grp] = []
      groups[grp].push(v)
    }
    for (const [label, list] of Object.entries(groups)) {
      if (!list.length) continue
      const g = document.createElement('optgroup')
      g.label = label
      for (const v of list) {
        const o = document.createElement('option')
        o.value = v.id; o.textContent = v.name
        if (v.id === ttsSt.voiceId) o.selected = true
        g.append(o)
      }
      sel.append(g)
    }
    if (!sel.value && voices.length) {
      const first = voices[0]
      sel.value = first.id; ttsSt.voiceId = first.id; ttsSt.voiceLang = first.lang
    }
  } catch (e) { console.error('ttsLoadVoices:', e) }
}
ttsLoadVoices()

$('#tts-btn').addEventListener('click', () => ttsSt.active ? ttsPauseResume() : ttsStart())
$('#tts-stop').addEventListener('click', ttsStop)
$('#tts-play').addEventListener('click', ttsPauseResume)
$('#tts-rate').addEventListener('change', (e) => {
  ttsSt.rate = parseFloat(e.target.value)
  if (ttsSt.active) {
    ttsSt.prefetch = {}
    const _ra = ttsSt.audio; ttsSt.audio = null
    if (_ra) { _ra.pause(); _ra.src = '' }
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
    ttsSpeakChunk()
  }
})
$('#tts-voice').addEventListener('change', (e) => {
  const v = ttsSt.allVoices.find(v => v.id === e.target.value)
  ttsSt.voiceId = v?.id || e.target.value; ttsSt.voiceLang = v?.lang || ''
  if (ttsSt.active) {
    ttsSt.prefetch = {}
    const _va = ttsSt.audio; ttsSt.audio = null
    if (_va) { _va.pause(); _va.src = '' }
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
    ttsSpeakChunk()
  }
})

// ===================== Настройки вида (UI) =====================
function syncSettingsUI() {
  document.querySelectorAll('.swatch').forEach((b) => b.setAttribute('aria-current', String(b.dataset.theme === prefs.theme)))
  $('#font-val').textContent = Math.round(prefs.fontScale * 100) + '%'
  $('#margin-val').textContent = MARGIN_NAME[prefs.marginLevel]
  $('#font-family').value = prefs.fontFamily
  $('#flow-mode').value = prefs.flow
  $('#columns-mode').value = String(prefs.columns || 1)
  // «Колонки» актуальны только в режиме страниц.
  $('#columns-row').style.display = prefs.flow === 'paginated' ? '' : 'none'
}
document.querySelectorAll('.swatch').forEach((b) => b.addEventListener('click', () => {
  prefs.theme = b.dataset.theme
  document.documentElement.dataset.theme = prefs.theme
  savePrefs(); syncSettingsUI(); applyViewStyles()
}))
$('#font-inc').addEventListener('click', () => { prefs.fontScale = Math.min(2, prefs.fontScale + 0.1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#font-dec').addEventListener('click', () => { prefs.fontScale = Math.max(0.6, prefs.fontScale - 0.1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#margin-inc').addEventListener('click', () => { prefs.marginLevel = Math.min(2, prefs.marginLevel + 1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#margin-dec').addEventListener('click', () => { prefs.marginLevel = Math.max(0, prefs.marginLevel - 1); savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#font-family').addEventListener('change', (e) => { prefs.fontFamily = e.target.value; savePrefs(); applyViewStyles() })
$('#flow-mode').addEventListener('change', (e) => { prefs.flow = e.target.value; savePrefs(); syncSettingsUI(); applyViewStyles() })
$('#columns-mode').addEventListener('change', (e) => { prefs.columns = parseInt(e.target.value, 10) || 1; savePrefs(); applyViewStyles() })

// ===================== Старт =====================
syncSettingsUI()
loadLibrary()
  .then(async () => {
    // Deep-link: /?open=<id> сразу открывает книгу в читалке.
    const openId = new URLSearchParams(location.search).get('open')
    if (openId) {
      const work = await api.get(`/api/library/${openId}`).catch(() => null)
      if (work) openReader(work)
    }
  })
  .catch((e) => { $('#ingest-status').hidden = false; $('#ingest-status').textContent = 'Сервер недоступен: ' + e.message })

$('#lib-filter').addEventListener('input', (e) => {
  applyLibFilter(e.target.value.trim())
})
