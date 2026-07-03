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

// Разобрать ответ-ошибку и бросить ApiError (тело читаем один раз).
async function fail(response, url, method) {
  const text = await response.text().catch(() => '')
  let data = null
  try { data = text ? JSON.parse(text) : null } catch {}
  throw new ApiError(response, url, method, text, data)
}

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export const api = {
  async get(url) {
    const r = await fetch(url)
    if (!r.ok) return fail(r, url, 'GET')
    return r.json()
  },
  async put(url, body) {
    const r = await fetch(url, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(body) })
    if (!r.ok) return fail(r, url, 'PUT')
    return r.json()
  },
  async post(url, body) {
    const r = await fetch(url, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) })
    if (!r.ok) return fail(r, url, 'POST')
    return r.json()
  },
  async delete(url) {
    const r = await fetch(url, { method: 'DELETE' })
    if (!r.ok) return fail(r, url, 'DELETE')
    return r.json()
  },
}
