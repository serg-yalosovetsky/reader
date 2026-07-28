// PDF → EPUB: старт конвертации на сервере и ожидание результата.
//
// Зачем: PDF — это макет страницы. foliate-js рендерит его через pdf.js, поэтому
// не работают ни размер шрифта, ни поля, ни темы, ни TTS, ни подсветки — читать
// с телефона нечем. Сервер один раз собирает EPUB (calibre) и дальше отдаёт его
// вместо PDF; оригинал остаётся доступен (кнопка «оригинал» / ?original=1).
import { api } from './api.js'

// Должно совпадать с CONVERTIBLE в backend/app/convert.py.
const CONVERTIBLE = new Set(['pdf', 'djvu', 'mobi', 'azw3', 'doc', 'docx', 'rtf'])

const PREF_KEY = 'reader:pdf-as-epub'

export function convertible(work) {
  return CONVERTIBLE.has(String(work?.file_format || '').toLowerCase())
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
