// Тонкая обёртка над fetch: JSON in/out.
// Ошибка HTTP → ApiError: .message = тело ответа (как раньше, для показа),
// плюс структурные поля .status/.statusText/.url/.method/.data (распарсенный JSON тела).
export class ApiError extends Error {
  constructor(response, url, method, bodyText, bodyData) {
    super(bodyText || `${response.status} ${response.statusText}`)
    this.name = 'ApiError'
    this.status = response.status
    this.statusText = response.statusText
    this.url = url
    this.method = method
    this.data = bodyData   // объект, если тело было JSON, иначе null
  }
}

// Сеть не дошла до сервера: обрыв, самолётный режим, мёртвый wifi. У браузера
// это голый `TypeError: Failed to fetch` без статуса — сообщение, которое
// пользователю ничего не говорит, а в интерфейс оно попадало дословно
// («Ошибка запуска: Failed to fetch»). Даём внятный текст и признак offline,
// сохраняя оригинал в .cause для консоли.
export class NetworkError extends Error {
  constructor(url, method, cause) {
    super('нет связи с сервером')
    this.name = 'NetworkError'
    this.url = url
    this.method = method
    this.cause = cause
    this.offline = !navigator.onLine
  }
}

// Сессия SSO протухла. nginx отдаёт на /api/* честный 401 с телом
// {error:"sso_required", login_url}, а НЕ 302 на sso.ibotz.fun: кросс-ориджин
// редирект браузер запрещает любому не-simple запросу (POST с JSON), и fetch
// падал в тот самый «Failed to fetch». Теперь это отдельный тип ошибки, и
// приложение может позвать перелогин вместо показа загадочного текста.
export class AuthRequiredError extends Error {
  constructor(url, method, loginUrl) {
    super('сессия истекла — нужно войти заново')
    this.name = 'AuthRequiredError'
    this.url = url
    this.method = method
    this.loginUrl = loginUrl || '/'
  }
}

// Подписчики на «сессия истекла» (регистрирует app.js). Держим списком, а не
// одним колбэком: перелогин — глобальное событие, на него может реагировать и
// баннер, и остановка фоновых поллеров.
const authListeners = []
export function onAuthRequired(fn) { authListeners.push(fn) }

let authNotified = false
function notifyAuthRequired(err) {
  // Один баннер на сессию: при протухшем SSO валятся ВСЕ параллельные запросы
  // (библиотека, прогресс, поллинг статуса) — без флага пользователь получил
  // бы десяток одинаковых сообщений подряд.
  if (authNotified) return
  authNotified = true
  for (const fn of authListeners) { try { fn(err) } catch { /* слушатель не должен ломать запрос */ } }
}

// Сообщить о протухшей сессии из кода, который ходит в сеть МИМО api.* (скачивание
// книги в офлайн стримит ответ вручную ради прогресса). Возвращает готовую ошибку,
// чтобы вызывающий её бросил — баннер поднимается здесь, в одном месте.
export function signalAuthRequired(url, method, loginUrl) {
  const err = new AuthRequiredError(url, method, loginUrl)
  notifyAuthRequired(err)
  return err
}

// Разобрать ответ-ошибку и бросить ApiError (тело читаем один раз).
async function fail(response, url, method) {
  const text = await response.text().catch(() => '')
  let data = null
  try { data = text ? JSON.parse(text) : null } catch {}
  if (response.status === 401 && data?.error === 'sso_required') {
    const err = new AuthRequiredError(url, method, data.login_url)
    notifyAuthRequired(err)
    throw err
  }
  throw new ApiError(response, url, method, text, data)
}

// fetch, у которого сетевой сбой — типизированная ошибка, а не голый TypeError.
async function request(url, method, init) {
  let r
  try {
    r = await fetch(url, init)
  } catch (e) {
    throw new NetworkError(url, method, e)
  }
  if (!r.ok) return fail(r, url, method)
  return r.json()
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export const api = {
  get(url) { return request(url, 'GET', undefined) },
  put(url, body) {
    return request(url, 'PUT', { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) })
  },
  post(url, body) {
    return request(url, 'POST', { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) })
  },
  delete(url) { return request(url, 'DELETE', { method: 'DELETE' }) },
}
