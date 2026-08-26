// Точка входа читалки: подключение модулей (слушатели навешиваются на импорте)
// и стартовая инициализация.
import '/vendor/foliate-js/view.js'
import { $ } from './core/dom.js'
import { api, onAuthRequired } from './core/api.js'
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
  ;(async () => {
    const work = await api.get(`/api/library/${_restore.id}`).catch(() => null)
    // Сбрасываем hash к «библиотеке», чтобы «назад» из книги вёл в неё, а не
    // застревал на #read/<id>; запись самой книги добавит openReader.
    if (_restore.fromHash) history.replaceState(null, '', location.pathname + location.search)
    if (!work) {
      $('#library').hidden = false  // книги нет — показываем библиотеку
      loadLibrary().catch(() => {})
      return
    }
    await openReader(work)
    // Библиотеку грузим ПОСЛЕ того, как книга открылась, и только в простое.
    // Раньше loadLibrary() стартовал первым «в фоне» — но фон тут условный:
    // список тянется секунды, а рендер ~1400 карточек занимает главный поток,
    // и открытие книги вставало в очередь за ним (замер: первые байты книги на
    // 1592-й мс). Читателю, пришедшему по ссылке на книгу, библиотека нужна
    // только для «назад», то есть заведомо позже.
    // ОБЯЗАТЕЛЬНО с timeout: без него requestIdleCallback может не сработать
    // вовсе (в неактивной вкладке он не вызывается), и библиотека осталась бы
    // незагруженной — «назад» показал бы пустой экран. Проверено: 61 секунда
    // без единого запроса списка.
    const idle = window.requestIdleCallback
      ? (f) => window.requestIdleCallback(f, { timeout: 1500 })
      : (f) => setTimeout(f, 400)
    idle(() => loadLibrary().catch(() => {}))
  })()
} else {
  loadLibrary()
    .catch((e) => { $('#ingest-status').hidden = false; $('#ingest-status').textContent = 'Сервер недоступен: ' + e.message })
}

// Протухшая SSO-сессия: показываем баннер с кнопкой входа. Без него всё, что
// требует сервера (обновления, прогресс, добавление книг), молча падало —
// библиотека при этом рисовалась из кэша service worker'а, и сайт выглядел
// рабочим. Баннер не автопереходит на sso: пользователь мог быть в середине
// главы, а редирект без спроса потерял бы позицию.
onAuthRequired((err) => {
  if (document.getElementById('sso-banner')) return
  const bar = document.createElement('div')
  bar.id = 'sso-banner'
  bar.setAttribute('role', 'alert')
  const text = document.createElement('span')
  text.textContent = 'Сессия истекла — данные не сохраняются и обновления не проверяются.'
  const link = document.createElement('a')
  link.href = err.loginUrl
  link.textContent = 'Войти'
  bar.append(text, link)
  document.body.appendChild(bar)
})

// Офлайн-режим: сверяем индекс кэшированных книг и регистрируем service
// worker (оболочка network-first: онлайн — свежий JS, офлайн — из кэша).
reconcileOffline()
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js')
      .catch((e) => console.warn('SW register failed', e))
  })
}
