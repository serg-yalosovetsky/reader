// Закладки (синк с сервером + Android).
import { $ } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { goToLocator } from './core/locator.js'
import { currentWork, view, lastCfi } from './core/state.js'
import { openPanel, closePanels } from './navigation.js'

// ===================== Закладки (синк с сервером + Android) =====================
// Сервер хранит locator как непрозрачную строку. Веб пишет foliate-CFI, Android —
// Readium-Locator JSON; общий якорь для кросс-девайс перехода — ratio. Поэтому при
// открытии закладки пробуем точный locator, а при неудаче откатываемся на goToFraction.
async function loadBookmarks() {
  if (!currentWork) return
  const list = $('#bm-list'); list.innerHTML = ''
  let items = []
  try { items = await api.get(`/api/bookmarks/${currentWork.id}`) } catch { items = [] }
  if (!items.length) {
    const p = document.createElement('p'); p.className = 'panel-empty'; p.textContent = 'Закладок пока нет'
    list.append(p); return
  }
  for (const bm of items) {
    const row = document.createElement('div'); row.className = 'bm-row'
    const a = document.createElement('a'); a.href = '#'; a.className = 'bm-link'
    const pct = Math.round((bm.ratio || 0) * 100)
    a.textContent = `${bm.label || 'Закладка'} · ${pct}%`
    a.addEventListener('click', async (ev) => {
      ev.preventDefault()
      await goToLocator(view, bm.locator, bm.ratio)
      closePanels()
    })
    const del = document.createElement('button')
    del.className = 'icon-btn bm-del'; del.textContent = '✕'; del.title = 'Удалить'
    del.addEventListener('click', async (ev) => {
      ev.stopPropagation(); ev.preventDefault()
      try { await fetch(`/api/bookmarks/id/${bm.id}`, { method: 'DELETE' }) } catch (e) { logErr('delete bookmark', e) }
      loadBookmarks()
    })
    row.append(a, del); list.append(row)
  }
}

async function addBookmarkHere() {
  if (!currentWork) return
  const ratio = parseFloat($('#progress-slider').value) || 0
  const label = `${Math.round(ratio * 100)}%`
  try {
    await api.post(`/api/bookmarks/${currentWork.id}`, { ratio, locator: lastCfi || '', label })
    loadBookmarks()
  } catch {}
}

$('#bm-btn').addEventListener('click', () => { openPanel('#bm-panel'); loadBookmarks() })
$('#bm-add').addEventListener('click', addBookmarkHere)
