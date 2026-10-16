// PDF → EPUB: старт конвертации на сервере и ожидание результата.
//
// Зачем: PDF — это макет страницы. foliate-js рендерит его через pdf.js, поэтому
// не работают ни размер шрифта, ни поля, ни темы, ни TTS, ни подсветки — читать
// с телефона нечем. Сервер один раз собирает EPUB (calibre) и дальше отдаёт его
// вместо PDF; оригинал остаётся доступен (кнопка «оригинал» / ?original=1).
import { api } from './api.js'
import { logErr } from './log.js'

// Должно совпадать с CONVERTIBLE в backend/app/convert.py.
const CONVERTIBLE = new Set(['pdf', 'djvu', 'mobi', 'azw3', 'doc', 'docx', 'rtf'])

const PREF_KEY = 'reader:pdf-as-epub'

export function convertible(work) {
  return CONVERTIBLE.has(String(work?.file_format || '').toLowerCase())
}

// Явный выбор важнее локальной истории, затем — сервер и общая настройка.
// Серверную свежесть сверяем отдельно: текущую книгу не переоткрываем.
export function formatRecord(work, record) {
  const format = String(record?.file_format || '').toLowerCase()
  const original = String(work?.file_format || '').toLowerCase()
  return convertible(work) && (format === original || format === 'epub')
    ? { file_format: format, saved_at: Number(record.saved_at) || 0 } : null
}

export function readingFormat(work, opts = {}, local = null, server = null, asEpub = true) {
  const original = String(work?.file_format || '').toLowerCase()
  if (!convertible(work)) return original
  if (opts.fmt === 'epub') return 'epub'
  if (opts.fmt === original || opts.original) return original
  return formatRecord(work, local)?.file_format || formatRecord(work, server)?.file_format
    || (asEpub ? 'epub' : original)
}

// Сверка позднего ответа влияет лишь на локальную историю следующего входа.
export function newerReadingFormat(work, local, server) {
  const a = formatRecord(work, local)
  const b = formatRecord(work, server)
  return b && (!a || b.saved_at > a.saved_at) ? b : a
}

const FORMAT_KEY = 'reader:last-format:'
export function localReadingFormat(work) {
  try {
    return formatRecord(work, JSON.parse(localStorage.getItem(FORMAT_KEY + work.id) || 'null'))
  } catch (e) {
    logErr('read local format', e)
    return null
  }
}

export function storeReadingFormat(work, record) {
  const valid = formatRecord(work, record)
  if (!valid) return
  try { localStorage.setItem(FORMAT_KEY + work.id, JSON.stringify(valid)) }
  catch (e) { logErr('save local format', e) }
}

// Обратный переход доступен только при готовой EPUB-версии.
export function formatSwitchTarget(work, format, epubReady = false) {
  if (!convertible(work) || !format) return null
  if (format === 'epub') return String(work.file_format).toLowerCase()
  return epubReady ? 'epub' : null
}

// Читать PDF как EPUB автоматически (по умолчанию — да).
export function pdfAsEpub() { return localStorage.getItem(PREF_KEY) !== '0' }
export function setPdfAsEpub(on) { localStorage.setItem(PREF_KEY, on ? '1' : '0') }

export function convertStatus(id) { return api.get(`/api/reader/${id}/convert`) }

// Запустить конвертацию и дождаться готового EPUB.
// onTick(sec, status) — для индикации; при таймауте возвращаем последний статус
// (конвертация продолжается на сервере, книга откроется готовой в следующий раз).
export async function ensureEpub(id, { force = false, timeoutMs = 240000, onTick } = {}) {
  let st = await api.post(`/api/reader/${id}/convert${force ? '?force=true' : ''}`, {})
  if (st.ready) return st
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    await new Promise((r) => setTimeout(r, 1500))
    st = await convertStatus(id).catch(() => st)
    if (st.ready || st.status === 'failed') return st
    try { onTick?.(Math.round((Date.now() - t0) / 1000), st) } catch { /* ignore */ }
  }
  return st
}
