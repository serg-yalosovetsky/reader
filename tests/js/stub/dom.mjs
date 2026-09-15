export const $ = () => null
export const toast = () => {}
// Копия настоящей escapeHtml из frontend/js/core/dom.js: панель скачиваний
// экранирует ею названия книг, и тест проверяет именно это.
export const escapeHtml = (s) => (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
