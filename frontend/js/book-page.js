// Страница книги: по клику на карточку показывается сводка (обложка, метаданные,
// жанры, описание) с кнопкой «Читать». И на десктопе, и на мобиле. Отдельный
// экран #book-page между библиотекой и читалкой.
import { $, escapeHtml, toast } from './core/dom.js'
import { api } from './core/api.js'
import { openReader } from './reader-core.js'
import { libProgress, libMonitored, libWorks } from './core/state.js'
import { uploadCoverFlow, copyCoverFlow } from './cover-picker.js'
import { coverUrl } from './core/coverpick.js'
import {
  offlineSupported, isOffline, downloadBook, removeBook, refreshBook, offlineMeta,
} from './core/offline.js'
import { filterBy, toggleHidden, isBookFinished, setBookFinished, updateBookIdentity } from './library.js'
import { convertible, convertStatus, ensureEpub } from './core/convert.js'
import { logErr } from './core/log.js'
import { chapterDate } from './core/chapterdate.js'
import { icon } from './core/icons.js'
import { renameBook } from './book-identity.js'

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

// Новая обложка книги: перерисовать её на странице и обновить cover_v в списке
// библиотеки — иначе после «назад» сетка показала бы старую картинку из кеша
// (обложки кешируются по URL с ?v=, service worker отдаёт их cache-first).
function applyCover(w, version) {
  w.cover_v = version
  if (curWork && curWork.id === w.id) curWork.cover_v = version
  const listed = libWorks.find((x) => x.id === w.id)
  if (listed) listed.cover_v = version
  const cov = $('.bp-cover')
  if (cov) {
    cov.innerHTML =
      `<img src="${coverUrl(w.id, version)}" alt="" onerror="this.remove()" />`
      + `<span class="bp-cover-fallback">${escapeHtml(w.title || 'Без названия')}</span>`
  }
}

function parseList(s) {
  if (!s) return []
  try { const a = JSON.parse(s); return Array.isArray(a) ? a : [] } catch { return [] }
}

// Кнопка = иконка + подпись. Подпись живёт в .bp-lbl: обработчики ниже меняют
// ТОЛЬКО её (setLbl), иначе textContent стёр бы иконку.
const btnInner = (ic, text) =>
  `<span class="bp-ic">${icon(ic)}</span><span class="bp-lbl">${text}</span>`
function setLbl(btn, text) {
  const l = btn.querySelector('.bp-lbl')
  if (l) l.textContent = text
  else btn.textContent = text
}
function getLbl(btn) { return btn.querySelector('.bp-lbl')?.textContent ?? btn.textContent }
function setIco(btn, name) {
  const i = btn.querySelector('.bp-ic')
  if (i) i.innerHTML = icon(name)
}

// Меню «Ещё»: редкие и необратимые действия. Одно на страницу, поэтому
// слушатели документа ставятся один раз (renderBookPage зовётся много раз).
function closeMenu(focusBtn) {
  const m = $('#bp-menu')
  const b = $('#bp-more')
  if (!m || !b || m.hidden) return
  m.hidden = true
  b.setAttribute('aria-expanded', 'false')
  b.closest('.bp-more')?.classList.remove('is-open')
  if (focusBtn) b.focus()
}
function openMenu() {
  const m = $('#bp-menu')
  const b = $('#bp-more')
  if (!m || !b) return
  m.hidden = false
  b.setAttribute('aria-expanded', 'true')
  b.closest('.bp-more')?.classList.add('is-open')
  m.querySelector('.bp-mi:not(:disabled)')?.focus()
}
document.addEventListener('click', (e) => {
  if (!e.target.closest?.('.bp-more')) closeMenu()
})
document.addEventListener('keydown', (e) => {
  const m = $('#bp-menu')
  if (!m || m.hidden) return
  if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
  const items = [...m.querySelectorAll('.bp-mi:not(:disabled)')]
  if (!items.length) return
  e.preventDefault()
  const i = items.indexOf(document.activeElement)
  let n = i
  if (e.key === 'ArrowDown') n = (i + 1) % items.length
  else if (e.key === 'ArrowUp') n = (i - 1 + items.length) % items.length
  else if (e.key === 'Home') n = 0
  else n = items.length - 1
  items[n].focus()
})

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
  // В URL — id книги: голый '#book' был ссылкой «в никуда», по ней (и после
  // перезагрузки вкладки) открывалась библиотека, а не книга (serg/tasks#1853).
  history.pushState({ bookpage: true, id: work.id }, '', `#book/${encodeURIComponent(work.id)}`)
  renderBookPage(work)                    // сразу из списочных данных
  window.scrollTo(0, 0)
  // Дотянуть полные данные (description хранится не в списке, а в детали).
  // Долю чтения — тоже, если списка библиотеки ещё нет: по ссылке #book/<id>
  // и после перезагрузки вкладки он грузится позже, и кнопка писала «Читать»
  // вместо «Продолжить · 99%».
  const needProg = !(work.id in libProgress)
  const [full, prog] = await Promise.all([
    api.get(`/api/library/${work.id}`).catch(() => null),
    needProg ? api.get(`/api/progress/${work.id}`).catch(() => null) : null,
  ])
  if (prog && prog.ratio > 0 && !(work.id in libProgress)) libProgress[work.id] = prog.ratio
  if (prog && 'finished_at' in prog && Boolean(prog.finished_at) !== isBookFinished(work.id)) {
    setBookFinished(work.id, Boolean(prog.finished_at))
  }
  if ((full || prog) && !$('#book-page').hidden && curWork && curWork.id === work.id) {
    curWork = { ...work, ...(full || {}) }
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
  // Документация Python «меряется» номером версии (3.14.7 → 31407): печатать
  // «30 гл. из 31407» — значит показать книгу недокачанной на 99.9%.
  const versioned = w.chapters_unit === 'version'
  const site = versioned ? 0 : (Number(w.chapters_site) || 0)
  // readli меряет СТРАНИЦАМИ пагинации — подписывать их главами нельзя.
  const unit = w.chapters_unit === 'pages' ? 'стр.' : 'гл.'
  const rows = []
  if (have || site) {
    const short = site && have && have < site
    rows.push(
      `<div class="bp-compl${short ? ' bp-compl-short' : ''}">`
      + `${icon(short ? 'alert-triangle' : 'circle-check', 16)}<span>Скачано: <b>${have || '—'}</b> ${unit}`
      + (site ? ` из <b>${site}</b>` : '')
      + (short ? ` <span class="bp-compl-note">— не хватает ${site - have}</span>` : '')
      + '</span></div>',
    )
  }
  if (offlineSupported && isOffline(w.id)) {
    const meta = offlineMeta(w.id)
    const off = meta && meta.chapters ? meta.chapters : 0
    const stale = off && have && have > off
    rows.push(
      `<div class="bp-compl${stale ? ' bp-compl-short' : ''}">`
      + `${icon(stale ? 'alert-triangle' : 'cloud-download', 16)}<span>В офлайне: <b>${off || '?'}</b> ${unit}`
      + (have ? ` из <b>${have}</b>` : '')
      + (stale ? ' <span class="bp-compl-note">— копия отстала, обнови</span>' : '')
      + '</span></div>',
    )
  }
  if (w.update_error) {
    rows.push(`<div class="bp-compl bp-compl-short">${icon('alert-triangle', 16)}<span>${escapeHtml(w.update_error)}</span></div>`)
  }
  return rows.length ? `<div class="bp-compl-box">${rows.join('')}</div>` : ''
}

// Подписаться можно только на книгу с адресом на сайте: у загруженных файлов
// и книг из Calibre проверять обновления негде.
function canWatch(w) {
  return /^https?:\/\//.test(w.source_url || '')
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
    ? `<div class="bp-series bp-link" data-flt-series title="Показать всю серию">Серия: ${escapeHtml(w.series)}${w.series_index ? ' #' + w.series_index : ''}</div>`
    : ''
  const descHtml = w.description
    ? `<div class="bp-desc">${escapeHtml(w.description).replace(/\n+/g, '<br>')}</div>`
    : ''
  const progHtml = ratio > 0
    ? `<div class="bp-progress" role="progressbar" aria-label="Прочитано" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(pct, 100)}"><div class="bp-progress-track"><i style="width:${Math.min(pct, 100)}%"></i></div></div>`
    : ''
  const origBtn = w.source_url
    ? `<a class="bp-mi" role="menuitem" href="${escapeHtml(w.source_url)}" target="_blank" rel="noopener">${btnInner('external-link', 'Открыть оригинал на сайте')}</a>`
    : ''
  const complHtml = completenessHtml(w)

  const finished = isBookFinished(w.id)
  const hasOff = offlineSupported && isOffline(w.id)
  // Главное действие одно и говорит, что будет: начать, продолжить или перечитать.
  const readLabel = finished ? 'Читать снова' : (ratio > 0 ? `Продолжить · ${pct}%` : 'Читать')

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
      </div>
      <div class="bp-actions">
        <button id="bp-read" type="button" class="bp-primary">${btnInner('book-2', readLabel)}</button>
        <div class="bp-sec">
          <button id="bp-finished" type="button" class="bp-sec-btn${finished ? ' is-on' : ''}" aria-pressed="${finished}" title="${finished ? 'Снять «прочитано»' : 'Отметить прочитанной'}. Отметка ставится сама на последней странице; здесь — вручную (книги с хвостом «об авторе»)">${btnInner(finished ? 'circle-check' : 'circle', 'Прочитана')}</button>
          ${w.monitored ? `<button id="bp-check" type="button" class="bp-sec-btn" aria-label="Проверить обновления" title="Спросить сайт, не появились ли новые главы">${btnInner('refresh', 'Проверить')}</button>` : ''}
          ${!w.monitored && canWatch(w) ? `<button id="bp-watch" type="button" class="bp-sec-btn" aria-label="Следить за обновлениями" title="Поставить книгу на автообновление и сразу проверить новые главы">${btnInner('bell', 'Следить')}</button>` : ''}
          ${convertible(w) ? `<button id="bp-read-orig" type="button" class="bp-sec-btn" title="Открыть исходный файл как есть, без EPUB-версии">${btnInner('file-text', `${escapeHtml(String(w.file_format || '').toUpperCase())} в оригинале`)}</button>` : ''}
          <a class="bp-sec-btn" href="/api/reader/${w.id}/file?original=1" download aria-label="Скачать файл книги">${btnInner('download', 'Скачать')}</a>
          <div class="bp-more">
            <button id="bp-more" type="button" class="bp-sec-btn bp-more-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="bp-menu" aria-label="Ещё: другие действия">${btnInner('dots', '<span class="bp-lbl-m">Ещё</span>')}</button>
            <div class="bp-scrim"></div>
            <div id="bp-menu" class="bp-menu" role="menu" aria-label="Другие действия с книгой" hidden>
              ${offlineSupported ? `<button id="bp-offline" type="button" class="bp-mi" role="menuitem" data-keep>${btnInner(hasOff ? 'refresh' : 'cloud-download', hasOff ? 'Обновить офлайн-копию' : 'Сохранить офлайн')}</button>` : ''}
              ${hasOff ? `<button id="bp-offline-rm" type="button" class="bp-mi" role="menuitem" title="Убрать книгу из офлайн-кэша">${btnInner('x', 'Убрать офлайн-копию')}</button>` : ''}
              ${origBtn}
              <button id="bp-rename" type="button" class="bp-mi" role="menuitem" title="Исправить название и автора">${btnInner('pencil', 'Переименовать')}</button>
              <button id="bp-mirror" type="button" class="bp-mi" role="menuitem" title="Указать другой адрес этой книги на сайте: книга переехала или выложена ещё где-то">${btnInner('link', 'Добавить зеркало')}</button>
              ${convertible(w) ? `
              <button id="bp-epub" type="button" class="bp-mi" role="menuitem" data-keep title="Собрать перетекающий текст вместо страниц-картинок">${btnInner('refresh', 'Сделать EPUB-версию')}</button>`
              : ''}
              <div class="bp-sep" role="separator"></div>
              <button id="bp-gencover" type="button" class="bp-mi" role="menuitem" data-keep>${btnInner('wand', 'Сгенерировать обложку')}</button>
              <button id="bp-uploadcover" type="button" class="bp-mi" role="menuitem" title="Выбрать картинку (JPEG, PNG, WebP до 10 МБ) с этого устройства">${btnInner('upload', 'Загрузить обложку')}</button>
              <button id="bp-copycover" type="button" class="bp-mi" role="menuitem" title="Скопировать обложку другой книги из библиотеки">${btnInner('copy', 'Взять обложку другой книги')}</button>
              <div class="bp-sep" role="separator"></div>
              <button id="bp-hide" type="button" class="bp-mi" role="menuitem" title="${w.hidden ? 'Вернуть книгу в библиотеку' : 'Убрать из библиотеки: останется доступной поиском'}">${btnInner(w.hidden ? 'eye' : 'eye-off', w.hidden ? 'Показать в библиотеке' : 'Скрыть из библиотеки')}</button>
              <div class="bp-sep bp-sep-danger" role="separator"></div>
              <button id="bp-del" type="button" class="bp-mi bp-mi-del" role="menuitem">${btnInner('trash', 'Удалить книгу…')}</button>
            </div>
          </div>
        </div>
      </div>
    </div>
    ${chipsHtml ? `<div class="bp-section"><div class="bp-section-h">Жанры и метки</div><div class="bp-chips">${chipsHtml}</div></div>` : ''}
    ${descHtml ? `<div class="bp-section"><div class="bp-section-h">Описание</div>${descHtml}</div>` : ''}
    <div class="bp-section" id="bp-toc-sec">
      <div class="bp-section-h">Оглавление <span id="bp-toc-count" class="bp-toc-count"></span></div>
      <input id="bp-toc-filter" class="bp-toc-filter" type="search" placeholder="Найти главу…"
             autocomplete="off" hidden aria-label="Поиск по оглавлению" />
      <div id="bp-toc" class="bp-toc">Загружаю оглавление…</div>
    </div>`

  $('#bp-rename').addEventListener('click', () => renameBook(w, (saved) => {
    Object.assign(w, saved)
    updateBookIdentity(saved)
    curWork = w
    renderBookPage(w)
  }))
  $('#bp-more').addEventListener('click', () => { if ($('#bp-menu').hidden) openMenu(); else closeMenu() })
  $('#bp-body .bp-scrim').addEventListener('click', () => closeMenu(true))
  $('#bp-menu').addEventListener('click', (e) => {
    const it = e.target.closest('.bp-mi')
    if (it && !it.hasAttribute('data-keep')) closeMenu()
  })
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
    const orig = getLbl(offBtn)
    const had = isOffline(w.id)
    offBtn.disabled = true
    // Немедленная обратная связь: раньше кнопка молчала до первого чанка, и на
    // медленном канале выглядела нерабочей — по ней жали снова и снова.
    setLbl(offBtn, 'Начинаю…')
    const onProgress = (rec, total) => {
      setLbl(offBtn, total
        ? `Сохраняю: ${Math.round((rec / total) * 100)}%`
        : `Сохраняю: ${Math.round(rec / 1048576)} МБ`)
    }
    const chapters = Number(w.chapters_have) || 0
    try {
      if (had) await refreshBook(w.id, onProgress, chapters)
      else await downloadBook(w.id, onProgress, chapters)
      toast(had ? 'Офлайн-копия обновлена' : 'Книга сохранена офлайн', 'ok')
      renderBookPage(w)     // перерисовать строку «В офлайне: N глав»
    } catch (e) {
      setLbl(offBtn, orig)
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
    setLbl(chkBtn, 'Проверяю…')
    try {
      const res = await api.post(`/api/monitored/check/${w.id}`)
      if (res.error) { toast(`Ошибка: ${res.error}`, 'err'); return }
      const full = await api.get(`/api/library/${w.id}`).catch(() => null)
      if (full) { curWork = { ...w, ...full }; w = curWork }
      // Ручная проверка перекачивает со всех зеркал (refetched): говорим, что
      // реально вышло, а не «новых глав нет» — счётчики сайта тут не показатель.
      const gained = res.refetched && (res.chapters || 0) > (res.chapters_before || 0)
      if (res.kept_existing) {
        toast('Зеркала не дали копии полнее — книга оставлена как есть', 'info')
      } else if (gained) {
        toast(`Докачано со всех зеркал: было ${res.chapters_before} гл., стало ${res.chapters}`, 'ok')
        if (isOffline(w.id)) {
          try { await refreshBook(w.id, null, Number(w.chapters_have) || 0) } catch { /* копия осталась старой */ }
        }
      } else if (res.refetched && !res.has_update && !res.error) {
        toast(`Проверены все зеркала — в книге ${res.chapters || w.chapters_have} гл., полнее нет`, 'info')
      } else if (res.downloaded) {
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
      setLbl(chkBtn, 'Проверить')
    }
  })
  // Книга без подписки (снята вручную, попала в чёрный список, пришла не через
  // скачивание) не обновлялась никак, а кнопки «проверить» у неё не было вовсе
  // (serg/tasks#1853). POST /api/monitored — штатное ручное добавление: оно же
  // снимает книгу с чёрного списка.
  $('#bp-watch')?.addEventListener('click', async () => {
    const btn = $('#bp-watch')
    btn.disabled = true
    setLbl(btn, 'Подписываю…')
    try {
      await api.post('/api/monitored', { url: w.source_url })
      const res = await api.post(`/api/monitored/check/${w.id}`).catch((e) => ({ error: e.message }))
      const full = await api.get(`/api/library/${w.id}`).catch(() => null)
      if (full) { curWork = { ...w, ...full }; w = curWork }
      libMonitored.add(w.id)
      if (res.error) toast(`Книга на отслеживании, но проверка не удалась: ${res.error}`, 'err')
      else if (res.downloaded) toast(`Книга на отслеживании — теперь ${res.chapters || w.chapters_have} гл.`, 'ok')
      else toast('Книга на отслеживании — новые главы будут подтягиваться сами', 'ok')
      renderBookPage(w)
    } catch (e) {
      btn.disabled = false
      setLbl(btn, 'Следить')
      if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
      else toast('Не удалось подписаться: ' + e.message, 'err')
    }
  })
  $('#bp-finished')?.addEventListener('click', async () => {
    const want = !isBookFinished(w.id)
    const btn = $('#bp-finished')
    btn.disabled = true
    try {
      await api.put(`/api/progress/${w.id}/finished`, { finished: want })
      setBookFinished(w.id, want)
      toast(want ? 'Отмечено прочитанной' : 'Отметка «прочитано» снята', 'ok')
      renderBookPage(w)
    } catch (e) {
      btn.disabled = false
      if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
      else toast('Не удалось изменить отметку: ' + e.message, 'err')
    }
  })
  // Ручное зеркало: фик переехал (ficbook выдал новый id, старая страница —
  // 404) или выложен ещё на одном сайте. Монитор будет проверять и этот адрес;
  // «основной» — ещё и для «Оригинала» и кнопки проверки (serg/tasks#1853).
  $('#bp-mirror')?.addEventListener('click', async () => {
    const url = (prompt('Ссылка на эту книгу на другом сайте или по новому адресу:') || '').trim()
    if (!url) return
    const primary = !canWatch(w) || confirm(
      'Сделать этот адрес основным источником книги?\n\n'
      + 'OK — «Оригинал» и проверка обновлений пойдут сюда (старый адрес умер).\n'
      + 'Отмена — оставить как дополнительное зеркало.')
    const btn = $('#bp-mirror')
    btn.disabled = true
    setLbl(btn, 'Добавляю…')
    try {
      await api.post(`/api/library/${w.id}/mirror`, { url, primary })
      libMonitored.add(w.id)
      setLbl(btn, 'Проверяю…')
      const res = await api.post(`/api/monitored/check/${w.id}`).catch((e) => ({ error: e.message }))
      const full = await api.get(`/api/library/${w.id}`).catch(() => null)
      if (full) { curWork = { ...w, ...full }; w = curWork }
      if (res.error) toast(`Зеркало добавлено, но проверка не удалась: ${res.error}`, 'err')
      else toast(`Зеркало добавлено — в книге ${res.chapters || w.chapters_have} гл.`, 'ok')
      renderBookPage(w)
    } catch (e) {
      btn.disabled = false
      setLbl(btn, 'Добавить зеркало')
      if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
      else toast('Не удалось добавить зеркало: ' + e.message, 'err')
    }
  })
  $('#bp-gencover')?.addEventListener('click', async () => {
    const btn = $('#bp-gencover')
    const orig = getLbl(btn)
    btn.disabled = true
    setLbl(btn, 'Генерирую…')
    try {
      const r = await fetch(`/api/reader/${w.id}/cover/generate?force=1`, { method: 'POST' })
      if (r.ok) {
        const d = await r.json()
        applyCover(w, d.cover_v)
      } else {
        alert('Не удалось сгенерировать обложку')
      }
    } catch (e) {
      alert('Ошибка генерации: ' + e.message)
    } finally {
      btn.disabled = false
      setLbl(btn, orig)
    }
  })
  // Ручная обложка (serg/tasks#1055): файл с устройства или обложка другой книги.
  $('#bp-uploadcover')?.addEventListener('click', () => uploadCoverFlow(w, (v) => applyCover(w, v)))
  $('#bp-copycover')?.addEventListener('click', () => copyCoverFlow(w, (v) => applyCover(w, v)))
  // PDF и прочая фиксированная вёрстка: EPUB-версия книги (перетекающий текст).
  // Кнопка показывает состояние и позволяет пересобрать, если вышло криво.
  const epubBtn = $('#bp-epub')
  if (epubBtn) {
    const paint = (st) => {
      if (!st) return
      if (st.status === 'pending') { setLbl(epubBtn, 'Собираю EPUB…'); epubBtn.disabled = true; return }
      epubBtn.disabled = false
      epubBtn.dataset.ready = st.ready ? '1' : ''
      if (st.ready) setLbl(epubBtn, 'EPUB готов — пересобрать')
      else if (st.status === 'failed') setLbl(epubBtn, 'EPUB не вышел — повторить')
      else setLbl(epubBtn, 'Сделать EPUB-версию')
    }
    convertStatus(w.id).then(paint).catch(() => {})
    epubBtn.addEventListener('click', async () => {
      const wasReady = epubBtn.dataset.ready === '1'
      epubBtn.disabled = true
      setLbl(epubBtn, 'Собираю EPUB…')
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
  // Скрыть книгу из библиотеки или вернуть (serg/tasks#923). Со страницы книги
  // не уходим: человек видит, что состояние переключилось.
  $('#bp-hide').addEventListener('click', async () => {
    const btn = $('#bp-hide')
    btn.disabled = true
    const ok = await toggleHidden(w)
    btn.disabled = false
    if (!ok) return
    setLbl(btn, w.hidden ? 'Показать в библиотеке' : 'Скрыть из библиотеки')
    setIco(btn, w.hidden ? 'eye' : 'eye-off')
    btn.title = w.hidden
      ? 'Вернуть книгу в библиотеку'
      : 'Убрать из библиотеки: останется доступной поиском'
  })
  // Оглавление подтягивается отдельным запросом: страница книги не должна
  // ждать разбора EPUB, а у файла в 181 главу список приходит не мгновенно.
  renderToc(w.id)
  loadToc(w.id)
  $('#bp-toc-filter')?.addEventListener('input', () => renderToc(w.id))
  $('#bp-del').addEventListener('click', async () => {
    if (!confirm(`Удалить «${w.title || 'книгу'}»?`)) return
    try {
      const r = await fetch(`/api/library/${w.id}`, { method: 'DELETE' })
      if (r.ok) { history.back() }
      else alert('Ошибка удаления')
    } catch (e) { alert('Ошибка удаления: ' + e.message) }
  })
}

// ===================== Оглавление книги =====================
// Список глав строит СЕРВЕР (backend/app/booktoc.py): тянуть ради заголовков
// весь файл книги на страницу-сводку — это мегабайты, особенно с телефона.
// Клик открывает читалку сразу на выбранной главе, а сохранённая позиция
// уезжает в историю переходов, чтобы её не потерять (serg/tasks#1078).
let tocCache = { id: null, items: null, error: '' }

async function loadToc(id, { force = false } = {}) {
  if (!force && tocCache.id === id && (tocCache.items || tocCache.error)) return
  tocCache = { id, items: null, error: '' }
  try {
    const data = await api.get(`/api/reader/${id}/toc`)
    if (tocCache.id !== id) return          // ушли на другую книгу
    tocCache.items = data?.items || []
    // Дат ещё нет — сервер пошёл за ними к источнику (это сетевой запрос на
    // десятки секунд). Один повтор: к этому времени они обычно уже в базе.
    if (data?.dates_pending && !force) {
      setTimeout(() => {
        if (curWork && curWork.id === id) loadToc(id, { force: true })
      }, 25000)
    }
  } catch (e) {
    if (tocCache.id !== id) return
    // Пустое оглавление и НЕДОСТУПНОЕ оглавление — разные вещи: молча показать
    // «глав нет» значило бы соврать про книгу, у которой они есть.
    tocCache.error = e.name === 'AuthRequiredError'
      ? 'сессия истекла — войдите заново'
      : (e.message || 'не удалось получить')
    logErr('оглавление книги не загрузилось', e)
  }
  renderToc(id)
}

function openChapter(item) {
  if (!curWork) return
  $('#book-page').hidden = true
  document.body.classList.remove('bookpage-open')
  openReader(curWork, {
    jump: { href: item.href || '', index: item.index, label: item.label || '' },
  })
}

function renderToc(id) {
  const box = $('#bp-toc')
  if (!box || !curWork || curWork.id !== id) return
  const countEl = $('#bp-toc-count')
  const filterEl = $('#bp-toc-filter')

  if (tocCache.id !== id || (!tocCache.items && !tocCache.error)) {
    box.textContent = 'Загружаю оглавление…'
    return
  }
  if (tocCache.error) {
    box.innerHTML = `<div class="bp-toc-empty">Оглавление не получено: ${escapeHtml(tocCache.error)}</div>`
    return
  }
  const items = tocCache.items || []
  if (!items.length) {
    box.innerHTML = '<div class="bp-toc-empty">У этого файла нет оглавления —'
      + ' главы можно листать в самой читалке.</div>'
    if (countEl) countEl.textContent = ''
    return
  }
  if (countEl) countEl.textContent = `${items.length} ${plural(items.length)}`
  if (filterEl) filterEl.hidden = items.length < 20   // на 7 главах поиск лишний

  const q = (filterEl?.value || '').trim().toLowerCase()
  const shown = q
    ? items.filter((it) => (it.label || '').toLowerCase().includes(q))
    : items
  if (!shown.length) {
    box.innerHTML = '<div class="bp-toc-empty">Ничего не нашлось</div>'
    return
  }
  box.innerHTML = ''
  shown.forEach((it, i) => {
    const a = document.createElement('a')
    a.href = '#'
    a.className = it.level ? 'bp-toc-item bp-toc-sub' : 'bp-toc-item'
    const when = chapterDate(it.date)
    a.innerHTML = `<span class="bp-toc-n">${items.indexOf(it) + 1}</span>`
      + `<span class="bp-toc-label">${escapeHtml(it.label || '—')}</span>`
      + (when ? `<span class="bp-toc-date">${escapeHtml(when)}</span>` : '')
    a.addEventListener('click', (ev) => { ev.preventDefault(); openChapter(it) })
    box.append(a)
  })
}

function plural(n) {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return 'глава'
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return 'главы'
  return 'глав'
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
