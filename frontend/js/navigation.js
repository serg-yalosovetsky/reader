// Навигация, панели, жесты/клавиши, полноэкранный, проверка обновлений книги.
import { $, toast } from './core/dom.js'
import { api } from './core/api.js'
import { prefs } from './core/prefs.js'
import { view, currentWork, bookDoc, lastIdx, libWorks, navStack,
         setView, setCurrentWork, setBookDoc, setSelIndex } from './core/state.js'
import { ttsStop } from './tts.js'
import { loadLibrary } from './library.js'
import { openReader, applyViewStyles } from './reader-core.js'
import { isOffline, removeBook, downloadBook } from './core/offline.js'
import { onSelectionChanged, hideSelPopup } from './highlights.js'

// ===================== Навигация и панели =====================
// Закрытие читалки → возврат в библиотеку (общая логика для кнопки и popstate).
function closeReader() {
  ttsStop()
  document.body.classList.remove('reader-open')
  $('#reader').hidden = true
  $('#library').hidden = false
  $('#search-results').innerHTML = ''; $('#search-meta').textContent = ''; $('#search-input').value = ''
  navStack.length = 0
  document.getElementById('reader')?.classList.remove('chrome-hidden')
  setCurrentWork(null); setView(null)
  loadLibrary()
}
$('#back-btn').addEventListener('click', () => {
  // Если открытие книги в истории — откатываемся через history.back(),
  // чтобы кнопка и браузерный «назад» вели себя одинаково (без накопления записей).
  if (history.state && history.state.reader) history.back()
  else closeReader()
})
// Браузерный «назад» из читалки: закрываем читалку, остаёмся на сайте.
window.addEventListener('popstate', () => {
  if (!$('#reader').hidden) closeReader()
})
// Между главами: всегда в НАЧАЛО целевой главы, в любом режиме.
async function gotoChapterStart(dir) {
  if (!view) return
  document.getElementById('reader')?.classList.remove('chrome-hidden')
  if (lastIdx == null) { dir > 0 ? view.next() : view.prev(); return }
  const target = lastIdx + dir
  if (target < 0) return
  try { await view.goTo(String(target)) } catch { dir > 0 ? view.next() : view.prev() }
}
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
  if (k === 'ArrowLeft') { view.prev(); e.preventDefault() }
  else if (k === 'ArrowRight') { view.next(); e.preventDefault() }
  else if (k === 'PageUp') { view.prev(); e.preventDefault() }
  else if (k === 'PageDown') { view.next(); e.preventDefault() }
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
export function attachKeysToDoc(e) {
  setBookDoc(e.detail.doc)
  setSelIndex(e.detail.index)
  try {
    // Выделение текста → плавающий попап (выделить/перевести/копировать).
    e.detail.doc.addEventListener('pointerup', () => setTimeout(() => onSelectionChanged(e.detail.doc), 10))
    e.detail.doc.addEventListener('selectionchange', () => {
      const s = e.detail.doc.getSelection?.(); if (!s || s.isCollapsed) hideSelPopup()
    })
    e.detail.doc.addEventListener('keydown', handleKey)
    e.detail.doc.addEventListener('wheel', (ev) => {
      if (prefs.flow === 'scrolled') {
        if (ev.deltaY < 0 && (view?.renderer?.start || 0) <= 2) {
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

    // Жесты «Ленты»: горизонтальный свайп листает главы (влево->предыд., вправо->след.);
    // вертикаль = чтение, смена главы только на доскролле за верх/низ.
    let _sx = 0, _sy = 0, _st = 0, _lastTY = null
    e.detail.doc.addEventListener('touchstart', (ev) => {
      const t = ev.changedTouches[0]; _sx = t.clientX; _sy = t.clientY; _st = Date.now(); _lastTY = t.clientY
    }, { passive: true })
    e.detail.doc.addEventListener('touchend', (ev) => {
            if (!view) return
      const t = ev.changedTouches[0]
      const dx = t.clientX - _sx, dy = t.clientY - _sy
      if (Date.now() - _st > 900) return
      const ax = Math.abs(dx), ay = Math.abs(dy)
      // Горизонталь -> между главами (в начало), в ЛЮБОМ режиме: влево=след., вправо=пред.
      if (ax > 40 && ax > ay * 1.2) { dx < 0 ? gotoChapterStart(1) : gotoChapterStart(-1); return }
      // Вертикаль в «Ленте» только на краях: верх -> конец пред. главы, низ -> начало след.
      if (prefs.flow === 'scrolled' && ay > 60 && ay > ax * 1.2) {
        const p = view?.renderer
        if (dy > 0 && (p?.start || 0) <= 2) { view.prev(); return }
        if (dy < 0 && p && (p.start + p.size) >= p.viewSize - 2) { view.next(); return }
      }
    }, { passive: true })


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

export function openPanel(id) { closePanels(); $(id).hidden = false; $('#panel-overlay').hidden = false }
export function closePanels() {
  $('#toc-panel').hidden = true; $('#settings-panel').hidden = true
  $('#search-panel').hidden = true; $('#bm-panel').hidden = true
  $('#hl-panel').hidden = true; $('#panel-overlay').hidden = true
}
$('#toc-btn').addEventListener('click', () => openPanel('#toc-panel'))
$('#settings-btn').addEventListener('click', () => openPanel('#settings-panel'))
$('#fs-btn')?.addEventListener('click', () => {
  const d = document
  if (d.fullscreenElement || d.webkitFullscreenElement) {
    (d.exitFullscreen || d.webkitExitFullscreen)?.call(d)
  } else {
    const el = d.documentElement
    ;(el.requestFullscreen || el.webkitRequestFullscreen)?.call(el)
  }
})
document.addEventListener('fullscreenchange', () => {
  const on = !!document.fullscreenElement
  const b = $('#fs-btn'); if (b) { b.textContent = on ? '✖' : '⛶'; b.title = on ? 'Выйти из полного экрана' : 'Полный экран' }
})
$('#update-btn').addEventListener('click', async () => {
  const btn = $('#update-btn')
  if (btn.dataset.state === 'checking' || !currentWork) return
  btn.dataset.state = 'checking'; btn.textContent = '↻'; btn.title = 'Проверяем...'
  const workId = currentWork?.id
  try {
    const res = await api.post(`/api/monitored/check/${workId}`)
    if (res.error) {
      const emsg = res.error === 'not_monitored' ? 'Книга не отслеживается' : `Ошибка: ${res.error}`
      btn.dataset.state = 'err'; btn.title = emsg
      toast(emsg, 'err')
      setTimeout(() => { btn.dataset.state = ''; btn.textContent = '↻'; btn.title = 'Проверить новые главы' }, 3500)
      return
    }
    if (res.downloaded) {
      btn.dataset.state = 'ok'; btn.title = `Загружено (${res.chapters_found} гл.)`
      toast(`Загружено новых глав до ${res.chapters_found} — открываю обновлённую книгу`, 'ok')
      // Освежаем офлайн-копию: openReader читает cache-first, и без
      // переустановки записи открылся бы СТАРЫЙ файл, а не докачанный.
      if (isOffline(workId)) {
        try { await removeBook(workId); await downloadBook(workId) } catch { /* офлайн-копия снята, книга откроется из сети */ }
      }
    } else if (res.has_update) {
      btn.dataset.state = 'err'; btn.title = 'Обновление есть, но скачать не удалось'
      toast('Обновление есть, но скачать не удалось. Попробуйте позже.', 'err')
    } else {
      btn.dataset.state = 'ok'; btn.title = 'Новых глав нет'
      toast('Новых глав нет — книга актуальна', 'info')
    }
    // Всегда перезагружаем epub: он мог обновиться плановым чеком пока книга была открыта
    await loadLibrary()
    const freshWork = libWorks.find(w => w.id === workId)
    if (freshWork) { await openReader(freshWork); return }
  } catch {
    btn.dataset.state = 'err'; btn.title = 'Ошибка проверки'
    toast('Не удалось проверить обновления (сеть?)', 'err')
  }
  setTimeout(() => { btn.dataset.state = ''; btn.textContent = '↻'; btn.title = 'Проверить новые главы' }, 3500)
})
$('#search-btn').addEventListener('click', () => { openPanel('#search-panel'); $('#search-input').focus() })
$('#panel-overlay').addEventListener('click', closePanels)
