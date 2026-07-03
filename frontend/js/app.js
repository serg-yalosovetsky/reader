// Точка входа читалки: подключение модулей (слушатели навешиваются на импорте)
// и стартовая инициализация.
import '/vendor/foliate-js/view.js'
import { $ } from './core/dom.js'
import { api } from './core/api.js'
import './core/prefs.js'
import { loadLibrary } from './library.js'
import { openReader } from './reader-core.js'
import { syncSettingsUI } from './settings.js'
// side-effect модули: навешивают DOM-слушатели своих секций
import './accounts.js'
import './navigation.js'
import './bookmarks.js'
import './highlights.js'
import './search.js'
import './tts.js'

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
