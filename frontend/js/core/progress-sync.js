// Надёжная запись позиции чтения (serg/tasks#1853).
//
// Раньше позиция уходила только PUT'ом из дебаунса (900 мс) и при закрытии
// читалки кнопкой. Свернули PWA, закрыли вкладку, пропала сеть — последнее место
// терялось, а упавший PUT просто писался в лог. Теперь:
//   1) каждая позиция СНАЧАЛА ложится в localStorage (синхронно — переживает
//      убийство вкладки), потом уходит на сервер;
//   2) ответ сервера снимает её из очереди, только если это та же запись;
//   3) неотправленное досылается при старте, при появлении сети и при открытии
//      книги — с флагом replay: сервер примет её, лишь если она новее своей.
import { logErr } from './log.js'

const KEY = 'reader.pendingProgress.v1'

function readAll() {
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {} } catch { return {} }
}
function writeAll(all) {
  try { localStorage.setItem(KEY, JSON.stringify(all)) } catch { /* квота/приватный режим — останется только PUT */ }
}

// Неотправленная позиция книги: { body, saved_at } или null.
export function pendingFor(workId) {
  return readAll()[workId] || null
}

function drop(workId, savedAt) {
  const all = readAll()
  if (all[workId] && all[workId].saved_at === savedAt) {
    delete all[workId]
    writeAll(all)
  }
}

// Записать позицию: очередь → PUT. keepalive — для pagehide/visibilitychange:
// такой запрос браузер доводит до сервера и после выгрузки страницы.
export function commitPosition(workId, pos, { keepalive = false, replay = false, savedAt } = {}) {
  const saved_at = savedAt || Date.now()
  const body = { ...pos, saved_at, replay }
  const all = readAll()
  all[workId] = { body, saved_at }
  writeAll(all)
  return send(workId, body, keepalive)
}

async function send(workId, body, keepalive) {
  try {
    const r = await fetch(`/api/progress/${workId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      keepalive,
      credentials: 'same-origin',
    })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    drop(workId, body.saved_at)
    return true
  } catch (e) {
    logErr('save progress (осталась в очереди)', e)
    return false
  }
}

// Дослать всё, что не ушло. Позиция в очереди старше той, что уже на сервере,
// там и отбросится (replay).
export async function replayPending() {
  const all = readAll()
  for (const [workId, rec] of Object.entries(all)) {
    if (!rec?.body) { drop(workId, rec?.saved_at); continue }
    await send(workId, { ...rec.body, replay: true }, false)
  }
}

// Время позиции на сервере в мс (клиентские часы того, кто её записал; для
// старых строк без saved_at — серверное время чтения).
export function serverSavedAt(prog) {
  if (!prog) return 0
  if (prog.saved_at) return Number(prog.saved_at)
  const t = prog.last_read_time
  if (!t) return 0
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t : t + 'Z') || 0
}

window.addEventListener('online', () => { replayPending() })
