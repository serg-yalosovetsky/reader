// DOM-утилиты: селектор + экранирование HTML.
export const $ = (s) => document.querySelector(s)
export const escapeHtml = (s) => (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
