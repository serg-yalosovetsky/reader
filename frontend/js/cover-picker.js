// Ручная обложка на странице книги (serg/tasks#1055): загрузить файл с устройства
// или взять обложку другой книги. Оба пути показывают предпросмотр и пишут на
// сервер только после «Заменить» — отмена ничего не меняет.
import { escapeHtml, toast } from './core/dom.js'
import { api, signalAuthRequired } from './core/api.js'
import { libWorks, libHidden } from './core/state.js'
import {
  checkCoverFile, pickableBooks, describeCoverError, coverUrl, COVER_PICK_LIMIT,
} from './core/coverpick.js'

const TYPES = 'image/jpeg,image/png,image/webp'

// Окно поверх страницы. Закрывается Esc, кликом по фону, крестиком и программно —
// и КАЖДЫЙ способ проходит через один close(), который вызывает onClose ровно
// один раз: иначе Esc оставлял бы ждущий результата код висеть навсегда. Фокус
// возвращается туда, откуда его взяли (с клавиатуры иначе оказываешься «в никуда»).
function openModal(title, bodyHtml, onClose) {
  const prevFocus = document.activeElement
  const overlay = document.createElement('div')
  overlay.className = 'modal-overlay cp-overlay'
  overlay.innerHTML = `
    <div class="modal cp-modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
      <div class="modal-head"><span>${escapeHtml(title)}</span>
        <button type="button" class="btn-ghost cp-x" aria-label="Закрыть">✕</button></div>
      <div class="cp-body">${bodyHtml}</div>
    </div>`
  document.body.appendChild(overlay)
  let closed = false
  const close = () => {
    if (closed) return
    closed = true
    document.removeEventListener('keydown', onKey, true)
    overlay.remove()
    if (prevFocus && prevFocus.focus) prevFocus.focus()
    if (onClose) onClose()
  }
  const onKey = (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close() }
  }
  document.addEventListener('keydown', onKey, true)
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close() })
  overlay.querySelector('.cp-x').addEventListener('click', close)
  return { overlay, body: overlay.querySelector('.cp-body'), close }
}

// Предпросмотр «сейчас → будет» с подтверждением. true — «Заменить», иначе false.
function confirmReplace({ title, currentSrc, newSrc, note }) {
  return new Promise((resolve) => {
    let yes = false
    const m = openModal(title, `
      <div class="cp-compare">
        <figure><div class="cp-thumb">${currentSrc ? `<img src="${currentSrc}" alt="" onerror="this.remove()" />` : ''}</div>
          <figcaption>Сейчас</figcaption></figure>
        <span class="cp-arrow" aria-hidden="true">→</span>
        <figure><div class="cp-thumb"><img src="${newSrc}" alt="" /></div>
          <figcaption>Будет</figcaption></figure>
      </div>
      ${note ? `<p class="cp-note">${escapeHtml(note)}</p>` : ''}
      <div class="cp-actions">
        <button type="button" class="btn-primary bp-btn" data-cp="ok">Заменить</button>
        <button type="button" class="btn-ghost bp-btn" data-cp="cancel">Отмена</button>
      </div>`, () => resolve(yes))
    m.body.querySelector('[data-cp=ok]').addEventListener('click', () => { yes = true; m.close() })
    m.body.querySelector('[data-cp=cancel]').addEventListener('click', m.close)
    m.body.querySelector('[data-cp=ok]').focus()
  })
}

async function readDetail(r) {
  try { return (await r.json()).detail } catch { return '' }
}

// Отправить файл. Возвращает {cover_v} или бросает Error с текстом для человека.
async function sendFile(work, file) {
  const url = `/api/reader/${work.id}/cover/upload`
  const fd = new FormData()
  fd.append('file', file, file.name || 'cover')
  let r
  try {
    r = await fetch(url, { method: 'POST', body: fd })
  } catch {
    throw new Error('нет связи с сервером')
  }
  if (r.status === 401) {
    const body = await r.clone().json().catch(() => null)
    if (body?.error === 'sso_required') throw signalAuthRequired(url, 'POST', body.login_url)
  }
  if (!r.ok) throw new Error(describeCoverError(r.status, await readDetail(r)))
  return r.json()
}

// Кнопка «Загрузить обложку». onDone(cover_v) — обновить страницу и список.
export function uploadCoverFlow(work, onDone) {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = TYPES
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0]
    if (!file) return
    const check = checkCoverFile(file)
    if (!check.ok) { toast(check.reason, 'err', 6000); return }
    const preview = URL.createObjectURL(file)
    try {
      const yes = await confirmReplace({
        title: 'Загрузить обложку',
        currentSrc: coverUrl(work.id, work.cover_v),
        newSrc: preview,
        note: file.name,
      })
      if (!yes) return
      const res = await sendFile(work, file)
      onDone(res.cover_v)
      toast('Обложка заменена', 'ok')
    } catch (e) {
      if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
      else toast(e.message || 'Не удалось загрузить обложку', 'err', 7000)
    } finally {
      URL.revokeObjectURL(preview)
    }
  })
  input.click()
}

// Окно выбора книги: поиск + миниатюры. Книга без обложки видна, но недоступна
// (картинка не загрузилась → `.cp-nocover`, кнопка disabled): выбрать её было бы
// заведомой ошибкой 409. Результат — выбранная книга или null.
function pickBook(work) {
  return new Promise((resolve) => {
    let chosen = null
    const m = openModal('Взять обложку из другой книги', `
      <input type="search" class="cp-search" placeholder="Название или автор"
             aria-label="Поиск книги" autocomplete="off" />
      <div class="cp-grid" role="list"></div>
      <div class="cp-hint" aria-live="polite"></div>`, () => resolve(chosen))
    const grid = m.body.querySelector('.cp-grid')
    const hint = m.body.querySelector('.cp-hint')
    const search = m.body.querySelector('.cp-search')
    const all = libWorks.concat(libHidden || [])

    const paint = () => {
      const list = pickableBooks(all, work.id, search.value)
      grid.innerHTML = list.map((w) => `
        <button type="button" role="listitem" class="cp-book" data-id="${w.id}"
                title="${escapeHtml(w.title || '')}">
          <span class="cp-thumb"><img loading="lazy" alt="" src="${coverUrl(w.id, w.cover_v, 320)}" /></span>
          <span class="cp-btitle">${escapeHtml(w.title || 'Без названия')}</span>
          <span class="cp-bauthor">${escapeHtml(w.author || '')}</span>
        </button>`).join('')
      hint.textContent = !list.length
        ? 'Ничего не найдено'
        : list.length >= COVER_PICK_LIMIT ? `Показаны первые ${COVER_PICK_LIMIT} — уточните запрос` : ''
      grid.querySelectorAll('img').forEach((img) => {
        img.addEventListener('error', () => {
          const b = img.closest('.cp-book')
          b.classList.add('cp-nocover')
          b.disabled = true
          b.title = 'У этой книги нет обложки'
          img.remove()
        }, { once: true })
      })
    }
    grid.addEventListener('click', (e) => {
      const b = e.target.closest('.cp-book')
      if (!b || b.disabled) return
      chosen = all.find((w) => w.id === Number(b.dataset.id)) || null
      m.close()
    })
    search.addEventListener('input', paint)
    paint()
    search.focus()
  })
}

// Кнопка «Взять обложку из другой книги».
export async function copyCoverFlow(work, onDone) {
  const src = await pickBook(work)
  if (!src) return
  try {
    const yes = await confirmReplace({
      title: 'Взять обложку из другой книги',
      currentSrc: coverUrl(work.id, work.cover_v),
      newSrc: coverUrl(src.id, src.cover_v),
      note: `Из книги «${src.title || 'без названия'}». Та книга не изменится.`,
    })
    if (!yes) return
    const url = `/api/reader/${work.id}/cover/copy-from/${src.id}`
    const res = await api.post(url, {}).catch((e) => {
      if (e.name === 'ApiError') {
        throw new Error(describeCoverError(e.status, e.data && e.data.detail))
      }
      throw e
    })
    onDone(res.cover_v)
    toast('Обложка заменена', 'ok')
  } catch (e) {
    if (e.name === 'AuthRequiredError') toast('Сессия истекла — войдите заново', 'err')
    else toast(e.message || 'Не удалось взять обложку', 'err', 7000)
  }
}
