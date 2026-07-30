// Точка входа читалки: подключение модулей (слушатели навешиваются на импорте)
// и стартовая инициализация.
import '/vendor/foliate-js/view.js'
import { $ } from './core/dom.js'
import { api } from './core/api.js'
import { reconcileOffline } from './core/offline.js'
import './core/prefs.js'
import { loadLibrary } from './library.js'
import { openReader } from './reader-core.js'
import { syncSettingsUI } from './settings.js'
// side-effect модули: навешивают DOM-слушатели своих секций
import './book-page.js'
import './accounts.js'
import './navigation.js'
import './bookmarks.js'
import './highlights.js'
import './search.js'
import './tts.js'

// ===================== Старт =====================
syncSettingsUI()
// Снимаем ранний класс-«заглушку» (его ставит инлайн-скрипт в <head>): дальше
// видимостью управляет JS. Держать класс нельзя — иначе «назад» в библиотеку
// оставит её скрытой (display:none победит .hidden=false).
document.documentElement.classList.remove('restoring-book')

// Восстановление вкладки: если URL указывает на книгу (#read/<id>) или это
// deep-link (?open=<id>) — открываем читалку СРАЗУ, параллельно с загрузкой
// библиотеки и НЕ показывая её. Иначе наполненная сетка мелькает ~секунду,
// пока loadLibrary дорендерится, и только потом поверх открывается книга.
const _restore = (() => {
  const h = (location.hash.match(/^#read\/(.+)$/) || [])[1]
  if (h) return { id: decodeURIComponent(h), fromHash: true }
  const q = new URLSearchParams(location.search).get('open')
  return q ? { id: q, fromHash: false } : null
})()

if (_restore) {
  // Прячем библиотеку до первого рендера сетки — читалка откроется поверх.
  $('#library').hidden = true
  // Библиотеку грузим в фоне: наполнит libMonitored/прогресс и будет готова к «назад».
  loadLibrary().catch(() => {})
  ;(async () => {
    const work = await api.get(`/api/library/${_restore.id}`).catch(() => null)
    // Сбрасываем hash к «библиотеке», чтобы «назад» из книги вёл в неё, а не
    // застревал на #read/<id>; запись самой книги добавит openReader.
    if (_restore.fromHash) history.replaceState(null, '', location.pathname + location.search)
    if (work) openReader(work)
    else $('#library').hidden = false  // книги нет — показываем библиотеку
  })()
} else {
  loadLibrary()
    .catch((e) => { $('#ingest-status').hidden = false; $('#ingest-status').textContent = 'Сервер недоступен: ' + e.message })
}

// Офлайн-режим: сверяем индекс кэшированных книг и регистрируем service
// worker (оболочка network-first: онлайн — свежий JS, офлайн — из кэша).
reconcileOffline()
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .catch((e) => console.warn('SW register failed', e))
  })
}
