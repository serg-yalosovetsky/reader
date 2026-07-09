// Страница книги: по клику на карточку показывается сводка (обложка, метаданные,
// жанры, описание) с кнопкой «Читать». И на десктопе, и на мобиле. Отдельный
// экран #book-page между библиотекой и читалкой.
import { $, escapeHtml } from './core/dom.js'
import { api } from './core/api.js'
import { openReader } from './reader-core.js'
import { libProgress, libMonitored } from './core/state.js'
import { offlineSupported, isOffline, downloadBook, removeBook } from './core/offline.js'

let curWork = null

const SITE_LABEL = {
  ficbook: 'ficbook.net', fanfics: 'fanfics.me', authortoday: 'author.today',
  ao3: 'AO3', ffn: 'fanfiction.net', calibre: 'Calibre', upload: 'Загружено',
}

function parseList(s) {
  if (!s) return []
  try { const a = JSON.parse(s); return Array.isArray(a) ? a : [] } catch { return [] }
}

function fmtNum(n) { return (n || 0).toLocaleString('ru-RU') }

function fmtDate(s) {
  if (!s) return ''
  const d = new Date(s)
  if (isNaN(d)) return ''
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' })
}

export async function openBookPage(work) {
  curWork = work
  $('#library').hidden = true
  $('#book-page').hidden = false
  document.body.classList.add('bookpage-open')
  // Всегда новая запись истории: «назад» из читалки/со страницы ведёт в библиотеку,
  // а следующий клик по книге не блокируется устаревшим bookpage-состоянием.
  history.pushState({ bookpage: true }, '', '#book')
  renderBookPage(work)                    // сразу из списочных данных
  window.scrollTo(0, 0)
  // Дотянуть полные данные (description хранится не в списке, а в детали).
  const full = await api.get(`/api/library/${work.id}`).catch(() => null)
  if (full && !$('#book-page').hidden && curWork && curWork.id === work.id) {
    curWork = { ...work, ...full }
    renderBookPage(curWork)
  }
}

export function closeBookPage() {
  $('#book-page').hidden = true
  $('#library').hidden = false
  document.body.classList.remove('bookpage-open')
  curWork = null
}

export function bookPageMeta(w) {
  // Компактная разметка метаданных — переиспользуется hover-панелью библиотеки.
  const genres = parseList(w.genres)
  const chips = genres.slice(0, 12).map(g => `<span class="bp-chip">${escapeHtml(g)}</span>`).join('')
  const badges = []
  if (w.rating) badges.push(`<span class="bp-badge bp-rating">${escapeHtml(w.rating)}</span>`)
  if (w.status) badges.push(`<span class="bp-badge bp-status">${escapeHtml(w.status)}</span>`)
  if (w.site && SITE_LABEL[w.site]) badges.push(`<span class="bp-badge bp-site">${escapeHtml(SITE_LABEL[w.site])}</span>`)
  const facts = []
  if (w.chapters_count) facts.push(`${w.chapters_count} гл.`)
  if (w.words) facts.push(`${fmtNum(w.words)} сл.`)
  const upd = fmtDate(w.updated_at)
  if (upd) facts.push(`обновлено ${upd}`)
  return { chipsHtml: chips, badgesHtml: badges.join(''), factsText: facts.join(' · ') }
}

function renderBookPage(w) {
  const ratio = libProgress[w.id] || 0
  const pct = Math.round(ratio * 100)
  const { chipsHtml, badgesHtml, factsText } = bookPageMeta(w)
  // Всегда запрашиваем /cover — бэкенд лениво сгенерирует обложку ИИ, если её
  // нет. Текст-фолбэк виден, пока грузится/если не удалось.
  const cover = `<img src="/api/reader/${w.id}/cover?v=${w.cover_v || 0}" alt="" onerror="this.remove()" />`
    + `<span class="bp-cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
  const authorHtml = w.source_url
    ? `<a class="bp-author" href="${escapeHtml(w.source_url)}" target="_blank" rel="noopener">${escapeHtml(w.author || 'Автор')}</a>`
    : `<span class="bp-author">${escapeHtml(w.author || '')}</span>`
  const descHtml = w.description
    ? `<div class="bp-desc">${escapeHtml(w.description).replace(/\n+/g, '<br>')}</div>`
    : ''
  const progHtml = ratio > 0
    ? `<div class="bp-progress"><div class="bp-progress-track"><i style="width:${Math.min(pct, 100)}%"></i></div><span>${pct}%</span></div>`
    : ''
  const origBtn = w.source_url
    ? `<a class="btn-ghost bp-btn" href="${escapeHtml(w.source_url)}" target="_blank" rel="noopener">↗ Оригинал</a>`
    : ''

  $('#bp-body').innerHTML = `
    <div class="bp-hero">
      <div class="bp-cover">${cover}</div>
      <div class="bp-info">
        <h1 class="bp-title">${escapeHtml(w.title || 'Без названия')}</h1>
        ${authorHtml}
        <div class="bp-badges">${badgesHtml}</div>
        ${factsText ? `<div class="bp-facts">${escapeHtml(factsText)}</div>` : ''}
        ${progHtml}
        <div class="bp-actions">
          <button id="bp-read" class="btn-primary bp-btn bp-btn-read">📖 Читать книгу</button>
          <a class="btn-ghost bp-btn" href="/api/reader/${w.id}/file" download>⬇ Скачать</a>
          ${offlineSupported ? `<button id="bp-offline" class="btn-ghost bp-btn">${isOffline(w.id) ? '✅ В офлайне' : '📥 Сохранить офлайн'}</button>` : ''}
          ${origBtn}
          <button id="bp-gencover" class="btn-ghost bp-btn">🎨 Сгенерировать обложку</button>
          <button id="bp-del" class="btn-ghost bp-btn bp-btn-del">🗑 Удалить</button>
        </div>
      </div>
    </div>
    ${chipsHtml ? `<div class="bp-section"><div class="bp-section-h">Жанры и метки</div><div class="bp-chips">${chipsHtml}</div></div>` : ''}
    ${descHtml ? `<div class="bp-section"><div class="bp-section-h">Описание</div>${descHtml}</div>` : ''}`

  $('#bp-read').addEventListener('click', () => {
    // Открываем читалку поверх — прячем страницу книги, читалка ведёт свою историю.
    $('#book-page').hidden = true
    document.body.classList.remove('bookpage-open')
    openReader(w)
  })
  // Сохранить/убрать книгу из офлайн-кэша (Cache API). С прогрессом закачки.
  const offBtn = $('#bp-offline')
  if (offBtn) offBtn.addEventListener('click', async () => {
    if (isOffline(w.id)) {
      await removeBook(w.id)
      offBtn.textContent = '📥 Сохранить офлайн'
      return
    }
    const orig = offBtn.textContent
    offBtn.disabled = true
    try {
      await downloadBook(w.id, (rec, total) => {
        offBtn.textContent = total
          ? `⬇ ${Math.round((rec / total) * 100)}%`
          : `⬇ ${Math.round(rec / 1048576)} МБ`
      })
      offBtn.textContent = '✅ В офлайне'
    } catch (e) {
      offBtn.textContent = orig
      alert('Не удалось сохранить офлайн: ' + e.message)
    } finally {
      offBtn.disabled = false
    }
  })
  $('#bp-gencover')?.addEventListener('click', async () => {
    const btn = $('#bp-gencover')
    const orig = btn.textContent
    btn.disabled = true
    btn.textContent = '🎨 Генерирую…'
    try {
      const r = await fetch(`/api/reader/${w.id}/cover/generate?force=1`, { method: 'POST' })
      if (r.ok) {
        const d = await r.json()
        w.cover_v = d.cover_v
        const cov = $('.bp-cover')
        if (cov) {
          cov.innerHTML =
            `<img src="/api/reader/${w.id}/cover?v=${d.cover_v}" alt="" onerror="this.remove()" />`
            + `<span class="bp-cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
        }
      } else {
        alert('Не удалось сгенерировать обложку')
      }
    } catch (e) {
      alert('Ошибка генерации: ' + e.message)
    } finally {
      btn.disabled = false
      btn.textContent = orig
    }
  })
  $('#bp-del').addEventListener('click', async () => {
    if (!confirm(`Удалить «${w.title || 'книгу'}»?`)) return
    try {
      const r = await fetch(`/api/library/${w.id}`, { method: 'DELETE' })
      if (r.ok) { history.back() }
      else alert('Ошибка удаления')
    } catch (e) { alert('Ошибка удаления: ' + e.message) }
  })
}

// Кнопка «назад» на странице книги.
$('#bp-back')?.addEventListener('click', () => {
  if (history.state && history.state.bookpage) history.back()
  else closeBookPage()
})

// Браузерный/аппаратный «назад» со страницы книги → библиотека.
window.addEventListener('popstate', () => {
  if (!$('#book-page').hidden) {
    closeBookPage()
    // перерисуем библиотеку (прогресс/удаления могли измениться)
    import('./library.js').then(m => m.loadLibrary()).catch(() => {})
  }
})
