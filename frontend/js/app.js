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
  { theme: 'day', fontScale: 1, marginLevel: 1, fontFamily: 'serif', flow: 'paginated' },
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
  const cover = w.cover_path
    ? `<img src="/api/reader/${w.id}/cover" alt="" />`
    : `<span class="cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
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
  applyViewStyles()
  buildTOC()

  // Восстановить позицию (locator = CFI). Иначе — начало текста.
  const prog = await api.get(`/api/progress/${work.id}`).catch(() => null)
  if (prog && prog.locator) await view.init({ lastLocation: prog.locator })
  else await view.init({ showTextStart: true })
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
  return `
    html { color: ${fg}; background: ${bg}; font-size: ${Math.round(prefs.fontScale * 100)}%; }
    body { font-family: ${fam}; }
    a:link, a:visited { color: ${accent}; }
    p, li, blockquote, dd { line-height: 1.55; text-align: justify; hyphens: auto; }
    img { max-width: 100%; height: auto; }
  `
}
function applyViewStyles() {
  if (!view) return
  view.renderer?.setStyles?.(bookCSS())
  view.renderer?.setAttribute('flow', prefs.flow)
  view.renderer?.setAttribute('max-inline-size', String(MARGIN_INLINE[prefs.marginLevel]))
  view.renderer?.setAttribute('gap', '6%')
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
document.addEventListener('keydown', (e) => {
  if ($('#reader').hidden) return
  if (e.key === 'ArrowLeft') view?.goLeft()
  else if (e.key === 'ArrowRight') view?.goRight()
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
$('#flow-mode').addEventListener('change', (e) => { prefs.flow = e.target.value; savePrefs(); applyViewStyles() })

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
