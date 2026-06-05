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
  { theme: 'day', fontScale: 1, marginLevel: 1, fontFamily: 'serif', flow: 'paginated', columns: 1 },
  JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'),
)
const savePrefs = () => localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))
const MARGIN_INLINE = { 0: 760, 1: 620, 2: 480 } // уровень полей → max-inline-size (меньше = шире поля)
const MARGIN_NAME = { 0: 'узк.', 1: 'сред.', 2: 'шир.' }

document.documentElement.dataset.theme = prefs.theme

// ===================== БИБЛИОТЕКА =====================
async function loadLibrary() {
  const works = await api.get('/api/library')
  const grid = $('#book-grid')
  grid.innerHTML = ''
  $('#lib-empty').hidden = works.length > 0
  for (const w of works) {
    const prog = await api.get(`/api/progress/${w.id}`).catch(() => ({ ratio: 0 }))
    grid.append(bookCard(w, prog.ratio || 0))
  }
}

function bookCard(w, ratio) {
  const card = document.createElement('div')
  card.className = 'book-card'
  const pct = Math.round((ratio || 0) * 100)
  const fallback = `<span class="cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
  const cover = w.cover_path
    ? `<img src="/api/reader/${w.id}/cover" alt="" onerror="this.remove()" />${fallback}`
    : fallback
  card.innerHTML = `
    <div class="book-cover">${cover}</div>
    <div class="book-meta">
      <div class="b-title">${escapeHtml(w.title || 'Без названия')}</div>
      <div class="b-author">${escapeHtml(w.author || '')}</div>
    </div>
    <div class="book-progress"><i style="width:${pct}%"></i></div>`
  card.addEventListener('click', () => openReader(w))
  return card
}

const escapeHtml = (s) => (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))

// Добавление по ссылке/названию (этап 2: /api/ingest). Пока эндпоинт может отсутствовать.
$('#ingest-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const q = $('#ingest-input').value.trim()
  if (!q) return
  const status = $('#ingest-status')
  status.hidden = false; status.classList.remove('error'); status.textContent = 'Скачиваю…'
  try {
    const work = await api.post('/api/ingest', { query: q })
    status.textContent = 'Готово: ' + (work.title || 'книга добавлена')
    $('#ingest-input').value = ''
    await loadLibrary()
  } catch (err) {
    status.classList.add('error')
    status.textContent = 'Скачивание появится на этапе 2. Пока используйте «Загрузить файл». (' + err.message.slice(0, 120) + ')'
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
$('#check-updates').addEventListener('click', async () => {
  accStatus('Проверяю обновления…')
  try {
    const r = await api.post('/api/monitored/check', {})
    accStatus(`Проверено: ${r.checked}, с обновлениями: ${r.with_updates}, докачано: ${r.downloaded}`)
    loadMonitored(); loadLibrary()
  } catch (err) { accStatus('Ошибка: ' + err.message.slice(0, 120), true) }
})

// ===================== ЧИТАЛКА =====================
let view = null
let currentWork = null
let saveTimer = null

async function openReader(work) {
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
}

// Применение темы/шрифта/полей к содержимому книги.
function resolvedColor(varName) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim()
}
function bookCSS() {
  const fg = resolvedColor('--fg'), bg = resolvedColor('--bg'), accent = resolvedColor('--accent')
  const fam = prefs.fontFamily === 'sans'
    ? 'var(--font-sans, system-ui, sans-serif)'
    : '"PT Serif", Georgia, "Times New Roman", serif'
  // В режиме «лента» одна колонка должна занимать всю ширину экрана.
  // Поля задаём уровнем «Поля» (marginLevel → процент боковых отступов).
  const sidePad = { 0: 4, 1: 8, 2: 14 }[prefs.marginLevel] ?? 8
  const scrolledBody = prefs.flow === 'scrolled'
    ? `html, body { max-width: none !important; width: auto !important; margin: 0 !important; }
       body { padding: 0 ${sidePad}% !important; }`
    : ''
  return `
    html { color: ${fg}; background: ${bg}; font-size: ${Math.round(prefs.fontScale * 100)}%; }
    body { font-family: ${fam}; }
    ${scrolledBody}
    a:link, a:visited { color: ${accent}; }
    p, li, blockquote, dd { line-height: 1.55; text-align: justify; hyphens: auto; }
    img { max-width: 100%; height: auto; }
  `
}
function applyViewStyles() {
  if (!view || !view.renderer) return
  const r = view.renderer
  r.setStyles?.(bookCSS())
  r.setAttribute('flow', prefs.flow)
  r.setAttribute('gap', '6%')
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
}

function buildTOC() {
  const toc = view?.book?.toc || []
  const list = $('#toc-list'); list.innerHTML = ''
  const add = (items, sub) => {
    for (const it of items) {
      const a = document.createElement('a')
      a.textContent = it.label || '—'
      if (sub) a.className = 'toc-sub'
      a.href = '#'
      a.addEventListener('click', (ev) => { ev.preventDefault(); view.goTo(it.href); closePanels() })
      list.append(a)
      if (it.subitems?.length) add(it.subitems, true)
    }
  }
  add(toc, false)
}

// ===================== Навигация и панели =====================
$('#back-btn').addEventListener('click', () => {
  $('#reader').hidden = true
  $('#library').hidden = false
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
function attachKeysToDoc(e) { try { e.detail.doc.addEventListener('keydown', handleKey) } catch {} }

// Переприменять раскладку при изменении размера окна (особенно ширину «ленты»).
let resizeTimer = null
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => { if (!$('#reader').hidden && view) applyViewStyles() }, 200)
})

function openPanel(id) { closePanels(); $(id).hidden = false; $('#panel-overlay').hidden = false }
function closePanels() { $('#toc-panel').hidden = true; $('#settings-panel').hidden = true; $('#panel-overlay').hidden = true }
$('#toc-btn').addEventListener('click', () => openPanel('#toc-panel'))
$('#settings-btn').addEventListener('click', () => openPanel('#settings-panel'))
$('#panel-overlay').addEventListener('click', closePanels)

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
