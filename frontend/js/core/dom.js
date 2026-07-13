// DOM-утилиты: селектор + экранирование HTML.
export const $ = (s) => document.querySelector(s)
export const escapeHtml = (s) => (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))


// Всплывающее уведомление (обратная связь по действиям — обновление книги и т.п.).
// kind: 'ok' | 'err' | 'info'. Клик — закрыть; авто-скрытие через ms.
export function toast(msg, kind = 'info', ms = 4200) {
  let box = document.getElementById('toast-box')
  if (!box) {
    box = document.createElement('div')
    box.id = 'toast-box'
    document.body.appendChild(box)
  }
  const el = document.createElement('div')
  el.className = `toast toast-${kind}`
  el.textContent = msg
  box.appendChild(el)
  requestAnimationFrame(() => el.classList.add('show'))
  const kill = () => { el.classList.remove('show'); setTimeout(() => el.remove(), 250) }
  const timer = setTimeout(kill, ms)
  el.addEventListener('click', () => { clearTimeout(timer); kill() })
}
