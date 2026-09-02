// Страница книги: по клику на карточку показывается сводка (обложка, метаданные,
// жанры, описание) с кнопкой «Читать». И на десктопе, и на мобиле. Отдельный
// экран #book-page между библиотекой и читалкой.
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { openReader } from './reader-core.js'
import { libProgress, libMonitored } from './core/state.js'
import {
  offlineSupported, isOffline, downloadBook, removeBook, refreshBook, offlineMeta,
} from './core/offline.js'
import { filterBy } from './library.js'
import { convertible, convertStatus, ensureEpub } from './core/convert.js'

let curWork = null

const SITE_LABEL = {
  ficbook: 'ficbook.net', fanfics: 'fanfics.me', authortoday: 'author.today',
  ao3: 'AO3', ffn: 'fanfiction.net', calibre: 'Calibre', upload: 'Загружено',
  readli: 'readli.net', searchfloor: 'searchfloor.org',
}

const HOST_LABEL = {
  'archiveofourown.org': 'AO3', 'fanfiction.net': 'fanfiction.net',
}

// Откуда книга ВЗЯТА, а не где лежит её файл. Поле site у десятков книг
// равно 'calibre' (так их пометила миграция на ссылки), хотя сама книга —
// фанфик с ficbook/author.today: бейдж «Calibre» вводил в заблуждение. source_url
// знает правду всегда, поэтому он важнее site.
function sourceLabel(w) {
  if (w.source_url) {
    try {
      const h = new URL(w.source_url).hostname.replace(/^www\./, '')
      return HOST_LABEL[h] || h
    } catch { /* битый URL — падаем на site ниже */ }
  }
  return (w.site && SITE_LABEL[w.site]) || ''
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

// Клик по автору/серии на странице книги: вернуться в библиотеку и отфильтровать.
function goFilter(value) {
  if (!value) return
  if (history.state && history.state.bookpage) history.back()
  else closeBookPage()
  filterBy(value)
}

export function bookPageMeta(w) {
  // Компактная разметка метаданных — переиспользуется hover-панелью библиотеки.
  const genres = parseList(w.genres)
  const chips = genres.slice(0, 12).map(g => `<span class="bp-chip">${escapeHtml(g)}</span>`).join('')
  const badges = []
  if (w.rating) badges.push(`<span class="bp-badge bp-rating">${escapeHtml(w.rating)}</span>`)
  if (w.status) badges.push(`<span class="bp-badge bp-status">${escapeHtml(w.status)}</span>`)
  const src = sourceLabel(w)
  if (src) badges.push(`<span class="bp-badge bp-site">${escapeHtml(src)}</span>`)
  // Файл хранится в Calibre — отдельный факт, не источник книги.
  if (w.calibre_id && src !== 'Calibre') badges.push('<span class="bp-badge bp-site">Calibre</span>')
  const facts = []
  if (w.chapters_count) facts.push(`${w.chapters_count} гл.`)
  if (w.words) facts.push(`${fmtNum(w.words)} сл.`)
  // «Обновлено» — про НОВЫЕ ГЛАВЫ, а не про наше чтение. updated_at двигает
  // любое сохранение прогресса (PUT /api/progress), и на странице книги это
  // читалось как «вчера вышли главы», хотя вчера её просто открывали.
  const upd = fmtDate(w.content_updated_at || w.updated_at)
  if (upd) facts.push(`обновлено ${upd}`)
  return { chipsHtml: chips, badgesHtml: badges.join(''), factsText: facts.join(' · ') }
}

// Строка полноты: сколько глав у нас, сколько на сайте и что из этого лежит
// в офлайне. Раньше этих чисел не было нигде, и недокачанная книга ничем не
// отличалась от полной — «Вечно голодный студент 9» простоял 21 главой из 25.
function completenessHtml(w) {
  const have = Number(w.chapters_have) || 0
  const site = Number(w.chapters_site) || 0
  // readli меряет СТРАНИЦАМИ пагинации — подписывать их главами нельзя.
  const unit = w.chapters_unit === 'pages' ? 'стр.' : 'гл.'
  const rows = []
  if (have || site) {
    const short = site && have && have < site
    rows.push(
      `<div class="bp-compl${short ? ' bp-compl-short' : ''}">`
      + `${short ? '⚠' : '✓'} Скачано: <b>${have || '—'}</b> ${unit}`
      + (site ? ` из <b>${site}</b>` : '')
      + (short ? ` <span class="bp-compl-note">— не хватает ${site - have}</span>` : '')
      + '</div>',
    )
  }
  if (offlineSupported && isOffline(w.id)) {
    const meta = offlineMeta(w.id)
    const off = meta && meta.chapters ? meta.chapters : 0
    const stale = off && have && have > off
    rows.push(
      `<div class="bp-compl${stale ? ' bp-compl-short' : ''}">`
      + `${stale ? '⚠' : '📥'} В офлайне: <b>${off || '?'}</b> ${unit}`
      + (have ? ` из <b>${have}</b>` : '')
      + (stale ? ' <span class="bp-compl-note">— копия отстала, обнови</span>' : '')
      + '</div>',
    )
  }
  if (w.update_error) {
    rows.push(`<div class="bp-compl bp-compl-short">⚠ ${escapeHtml(w.update_error)}</div>`)
  }
  return rows.length ? `<div class="bp-compl-box">${rows.join('')}</div>` : ''
}

function renderBookPage(w) {
  const ratio = libProgress[w.id] || 0
  const pct = Math.round(ratio * 100)
  const { chipsHtml, badgesHtml, factsText } = bookPageMeta(w)
  // Всегда запрашиваем /cover — бэкенд лениво сгенерирует обложку ИИ, если её
  // нет. Текст-фолбэк виден, пока грузится/если не удалось.
  const cover = `<img src="/api/reader/${w.id}/cover?v=${w.cover_v || 0}" alt="" onerror="this.remove()" />`
    + `<span class="bp-cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
  const authorHtml = w.author
    ? `<span class="bp-author bp-link" data-flt-author title="Показать все книги автора">${escapeHtml(w.author)}</span>`
    : `<span class="bp-author"></span>`
  const seriesHtml = w.series
    ? `<div class="bp-series bp-link" data-flt-series title="Показать всю серию">📚 ${escapeHtml(w.series)}${w.series_index ? ' #' + w.series_index : ''}</div>`
    : ''
  const descHtml = w.description
    ? `<div class="bp-desc">${escapeHtml(w.description).replace(/\n+/g, '<br>')}</div>`
    : ''
  const progHtml = ratio > 0
    ? `<div class="bp-progress"><div class="bp-progress-track"><i style="width:${Math.min(pct, 100)}%"></i></div><span>${pct}%</span></div>`
    : ''
  const origBtn = w.source_url
    ? `<a class="btn-ghost bp-btn" href="${escapeHtml(w.source_url)}" target="_blank" rel="noopener">↗ Оригинал</a>`
    : ''
  const complHtml = completenessHtml(w)

  $('#bp-body').innerHTML = `
    <div class="bp-hero">
      <div class="bp-cover">${cover}</div>
      <div class="bp-info">
        <h1 class="bp-title">${escapeHtml(w.title || 'Без названия')}</h1>
        ${authorHtml}
        ${seriesHtml}
        <div class="bp-badges">${badgesHtml}</div>
        ${factsText ? `<div class="bp-facts">${escapeHtml(factsText)}</div>` : ''}
        ${complHtml}
        ${progHtml}
        <div class="bp-actions">
          <button id="bp-read" class="btn-primary bp-btn bp-btn-read">📖 Читать книгу</button>
          <a class="btn-ghost bp-btn" href="/api/reader/${w.id}/file?original=1" download>⬇ Скачать</a>
          ${convertible(w) ? `
          <button id="bp-epub" class="btn-ghost bp-btn" title="Собрать перетекающий текст вместо страниц-картинок">↻ EPUB-версия</button>
          <button id="bp-read-orig" class="btn-ghost bp-btn" title="Открыть исходный файл как есть">📄 Читать оригинал</button>`
          : ''}
          ${w.monitored ? `<button id="bp-check" class="btn-ghost bp-btn" title="Спросить сайт, не появились ли новые главы">↻ Проверить обновления</button>` : ''}
          ${offlineSupported ? `<button id="bp-offline" class="btn-ghost bp-btn">${isOffline(w.id) ? '🔄 Обновить офлайн' : '📥 Сохранить офлайн'}</button>` : ''}
          ${offlineSupported && isOffline(w.id) ? '<button id="bp-offline-rm" class="btn-ghost bp-btn" title="Убрать книгу из офлайн-кэша">🗑 Убрать офлайн</button>' : ''}
          ${origBtn}
          <button id="bp-gencover" class="btn-ghost bp-btn">🎨 Сгенерировать обложку</button>
          <button id="bp-del" class="btn-ghost bp-btn bp-btn-del">🗑 Удалить</button>
        </div>
      </div>
    </div>
    ${chipsHtml ? `<div class="bp-section"><div class="bp-section-h">Жанры и метки</div><div class="bp-chips">${chipsHtml}</div></div>` : ''}
    ${descHtml ? `<div class="bp-section"><div class="bp-section-h">Описание</div>${descHtml}</div>` : ''}`

  $('#bp-body').querySelectorAll('[data-flt-author]').forEach((el) =>
    el.addEventListener('click', () => goFilter(w.author)))
  $('#bp-body').querySelectorAll('[data-flt-series]').forEach((el) =>
    el.addEventListener('click', () => goFilter(w.series)))

  $('#bp-read').addEventListener('click', () => {
    // Открываем читалку поверх — прячем страницу книги, читалка ведёт свою историю.
    $('#book-page').hidden = true
    document.body.classList.remove('bookpage-open')
    openReader(w)
  })
  // Сохранить/убрать книгу из офлайн-кэша (Cache API). С прогрессом закачки.
  // Сохранить/обновить офлайн-копию. У уже сохранённой книги это ОБНОВЛЕНИЕ
  // (перекачка поверх), а не удаление: сервер мог докачать главы, а SW отдаёт
  // книгу cache-first — без перекачки новых глав не увидеть вовсе.
  const offBtn = $('#bp-offline')
  if (offBtn) offBtn.addEventListener('click', async () => {
    const orig = offBtn.textContent
    const had = isOffline(w.id)
    offBtn.disabled = true
    // Немедленная обратная связь: раньше кнопка молчала до первого чанка, и на
    // медленном канале выглядела нерабочей — по ней жали снова и снова.
    offBtn.textContent = '⏳ Начинаю…'
    const onProgress = (rec, total) => {
      offBtn.textContent = total
        ? `⬇ ${Math.round((rec / total) * 100)}%`
        : `⬇ ${Math.round(rec / 1048576)} МБ`
    }
    const chapters = Number(w.chapters_have) || 0
    try {
      if (had) await refreshBook(w.id, onProgress, chapters)
      else await downloadBook(w.id, onProgress, chapters)
      toast(had ? 'Офлайн-копия обновлена' : 'Книга сохранена офлайн', 'ok')
      renderBookPage(w)     // перерисовать строку «В офлайне: N глав»
    } catch (e) {
      offBtn.textContent = orig
      offBtn.disabled = false
      // AuthRequiredError сам поднимает баннер перелогина — не дублируем алертом.
      if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
      else toast('Не удалось сохранить офлайн: ' + e.message, 'err')
    }
  })
  $('#bp-offline-rm')?.addEventListener('click', async () => {
    await removeBook(w.id)
    renderBookPage(w)
  })
  // Проверка обновлений прямо со страницы книги: раньше кнопка ↻ жила только в
  // тулбаре читалки, то есть узнать о новых главах можно было лишь открыв книгу.
  const chkBtn = $('#bp-check')
  if (chkBtn) chkBtn.addEventListener('click', async () => {
    chkBtn.disabled = true
    chkBtn.textContent = '↻ Проверяю…'
    try {
      const res = await api.post(`/api/monitored/check/${w.id}`)
      if (res.error) { toast(`Ошибка: ${res.error}`, 'err'); return }
      const full = await api.get(`/api/library/${w.id}`).catch(() => null)
      if (full) { curWork = { ...w, ...full }; w = curWork }
      if (res.downloaded) {
        toast(`Загружено — теперь ${res.chapters || w.chapters_have} гл.`, 'ok')
        // Офлайн-копия обязана догнать: иначе cache-first отдаст старый файл.
        if (isOffline(w.id)) {
          try { await refreshBook(w.id, null, Number(w.chapters_have) || 0) } catch { /* копия осталась старой */ }
        }
      } else if (res.has_update) {
        toast('Обновление есть, но скачать не удалось', 'err')
      } else {
        toast('Новых глав нет — книга актуальна', 'info')
      }
      renderBookPage(w)
    } catch (e) {
      if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
      else toast('Не удалось проверить: ' + e.message, 'err')
    } finally {
      chkBtn.disabled = false
      chkBtn.textContent = '↻ Проверить обновления'
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
  // PDF и прочая фиксированная вёрстка: EPUB-версия книги (перетекающий текст).
  // Кнопка показывает состояние и позволяет пересобрать, если вышло криво.
  const epubBtn = $('#bp-epub')
  if (epubBtn) {
    const paint = (st) => {
      if (!st) return
      if (st.status === 'pending') { epubBtn.textContent = '⏳ Собираю EPUB…'; epubBtn.disabled = true; return }
      epubBtn.disabled = false
      if (st.ready) epubBtn.textContent = '✅ EPUB готов — пересобрать'
      else if (st.status === 'failed') epubBtn.textContent = '⚠ EPUB не вышел — повторить'
      else epubBtn.textContent = '↻ Сделать EPUB-версию'
    }
    convertStatus(w.id).then(paint).catch(() => {})
    epubBtn.addEventListener('click', async () => {
      const wasReady = epubBtn.textContent.startsWith('✅')
      epubBtn.disabled = true
      epubBtn.textContent = '⏳ Собираю EPUB…'
      const st = await ensureEpub(w.id, { force: wasReady }).catch((e) => ({ status: 'failed', error: e.message }))
      paint(st)
      if (st.ready) toast('EPUB готов — книга откроется перетекающим текстом', 'ok')
      else if (st.status === 'failed') toast('Не удалось собрать EPUB: ' + (st.error || ''), 'err', 8000)
      else toast('Конвертация продолжается на сервере', 'info')
    })
  }
  $('#bp-read-orig')?.addEventListener('click', () => {
    $('#book-page').hidden = true
    document.body.classList.remove('bookpage-open')
    openReader(w, { original: true })
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
