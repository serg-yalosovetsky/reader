// Ручная обложка: чистая логика без DOM (serg/tasks#1055).
//
// Вынесена в модуль БЕЗ импортов по той же причине, что core/keynav.js и
// core/fullscreen.js: cover-picker.js тянет DOM и сеть и в node не грузится, а
// решения «годится ли файл», «какие книги показать» и «что сказать человеку при
// отказе» должны проверяться тестом.
//
// Клиентская проверка — только быстрый отказ до отправки; последнее слово за
// сервером (он смотрит на содержимое, а не на то, что назвал браузер).

export const COVER_MAX_BYTES = 10 * 1024 * 1024
export const COVER_PICK_LIMIT = 60

// Файл годится для отправки? Тип, который сообщает браузер, у части Android-
// файловых менеджеров пуст — пустой тип не отбраковываем, решит сервер.
export function checkCoverFile(file) {
  if (!file) return { ok: false, reason: 'Файл не выбран' }
  if (file.size > COVER_MAX_BYTES) {
    return { ok: false, reason: `Файл больше ${COVER_MAX_BYTES / (1024 * 1024)} МБ` }
  }
  if (!file.size) return { ok: false, reason: 'Файл пустой' }
  if (file.type && !file.type.startsWith('image/')) {
    return { ok: false, reason: 'Нужна картинка JPEG, PNG или WebP' }
  }
  return { ok: true, reason: '' }
}

// Книга подходит под запрос? Каждое слово запроса должно встретиться в названии
// или авторе (регистр не важен, «ё» = «е»), порядок слов не важен.
const fold = (s) => (s || '').toLowerCase().replace(/ё/g, 'е')

export function matchBook(work, query) {
  const words = fold(query).split(/\s+/).filter(Boolean)
  if (!words.length) return true
  const hay = fold(`${work.title || ''} ${work.author || ''}`)
  return words.every((w) => hay.includes(w))
}

// Книги для окна выбора: без самой книги, по запросу, не больше limit.
export function pickableBooks(works, excludeId, query, limit = COVER_PICK_LIMIT) {
  const out = []
  for (const w of works) {
    if (w.id === excludeId || !matchBook(w, query)) continue
    out.push(w)
    if (out.length >= limit) break
  }
  return out
}

// Что сказать человеку, когда сервер отказал. detail — поле `detail` тела ответа.
export function describeCoverError(status, detail) {
  const text = typeof detail === 'string' ? detail : ''
  switch (status) {
    case 400: return text || 'Нельзя взять обложку у той же книги'
    case 404: return text || 'Книга не найдена'
    case 409: return 'У выбранной книги нет обложки'
    case 413: return text || 'Файл слишком большой'
    case 415: return text || 'Нужна картинка JPEG, PNG или WebP'
    case 422: return text || 'Картинка повреждена или слишком маленькая'
    default: return text || `Не удалось сохранить обложку (${status})`
  }
}

// Адрес обложки с версией: версия — единственное, что заставляет браузер и
// service worker (cache-first по URL) показать новую картинку.
export function coverUrl(id, version, width = 0) {
  return `/api/reader/${id}/cover?${width ? `w=${width}&` : ''}v=${version || 0}`
}
