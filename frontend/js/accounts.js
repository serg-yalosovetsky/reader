// Аккаунты сайтов + проверка обновлений подписок.
import { $, escapeHtml } from './core/dom.js'
import { api } from './core/api.js'
import { loadLibrary } from './library.js'

// ===================== Аккаунты и обновления =====================
const accStatus = (msg, err) => {
  const el = $('#accounts-status'); el.hidden = false
  el.classList.toggle('error', !!err); el.textContent = msg
}
async function loadAccounts() {
  const accs = await api.get('/api/accounts').catch(() => [])
  const box = $('#accounts-list'); box.innerHTML = ''
  if (!accs.length) box.innerHTML = '<div class="acc-row">Аккаунтов нет</div>'
  for (const a of accs) {
    const row = document.createElement('div'); row.className = 'acc-row'
    row.innerHTML = `<span>${escapeHtml(a.site)} — ${escapeHtml(a.username)}</span>`
    const del = document.createElement('button'); del.className = 'icon-btn'; del.textContent = '✕'
    del.addEventListener('click', async () => { await fetch(`/api/accounts/${a.id}`, { method: 'DELETE' }); loadAccounts() })
    row.append(del); box.append(row)
  }
}
async function loadMonitored() {
  const items = await api.get('/api/monitored').catch(() => [])
  const box = $('#monitored-list'); box.innerHTML = ''
  if (!items.length) { box.innerHTML = '<div class="mon-row">Пока ничего не отслеживается</div>'; return }
  for (const m of items) {
    const row = document.createElement('div'); row.className = 'mon-row' + (m.has_update ? ' has-update' : '')
    const name = m.title || m.source_url
    row.innerHTML = `<span class="mon-title">${escapeHtml(name)}</span>` +
      `<span>${m.last_seen_chapters} гл.${m.has_update ? ' <span class="badge">обновление</span>' : ''}</span>`
    box.append(row)
  }
}
$('#accounts-btn').addEventListener('click', () => {
  $('#accounts-overlay').hidden = false; loadAccounts(); loadMonitored()
})
// Мобильный тоггл «Ещё»: раскрывает/сворачивает вторичные действия библиотеки
$('#more-actions-btn')?.addEventListener('click', () => {
  const acts = $('.lib-actions')
  const open = acts.classList.toggle('more-open')
  $('#more-actions-btn').setAttribute('aria-expanded', String(open))
})
$('#accounts-close').addEventListener('click', () => { $('#accounts-overlay').hidden = true })
$('#accounts-overlay').addEventListener('click', (e) => { if (e.target.id === 'accounts-overlay') $('#accounts-overlay').hidden = true })
$('#account-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const site = $('#acc-site').value, username = $('#acc-user').value.trim(), password = $('#acc-pass').value
  if (!username || !password) { accStatus('Введите логин и пароль', true); return }
  try {
    await api.post('/api/accounts', { site, username, password })
    $('#acc-user').value = ''; $('#acc-pass').value = ''
    accStatus('Аккаунт сохранён'); loadAccounts()
  } catch (err) { accStatus('Ошибка: ' + err.message.slice(0, 120), true) }
})
// Показ ошибок последней проверки прямо в модалке: ошибки логина по сайтам
// (result.feeds[site].error, напр. author.today email-код) и по-фиковые ошибки
// докачки (result.details[].error). Контейнер создаём один раз под статусом.
function renderUpdateErrors(r) {
  let box = $('#update-errors')
  if (!box) {
    box = document.createElement('div')
    box.id = 'update-errors'
    box.style.cssText = 'margin-top:8px;font-size:13px;color:#d9534f;max-height:200px;overflow:auto'
    const anchor = $('#accounts-status')
    anchor.parentNode.insertBefore(box, anchor.nextSibling)
  }
  const feedErrs = Object.entries((r && r.feeds) || {})
    .filter(([, v]) => v && v.error)
    .map(([site, v]) => `<div>⚠ <b>${escapeHtml(site)}</b>: ${escapeHtml(String(v.error))}</div>`)
  const dlErrs = ((r && r.details) || [])
    .filter(d => d && d.error)
    .slice(0, 40)
    .map(d => `<div>⚠ ${escapeHtml(String(d.url || '').replace(/^https?:\/\//, ''))}: ${escapeHtml(String(d.error))}</div>`)
  const all = feedErrs.concat(dlErrs)
  if (!all.length) { box.hidden = true; box.innerHTML = ''; return }
  box.hidden = false
  box.innerHTML = `<div style="font-weight:600;margin-bottom:4px">Ошибки (${all.length}):</div>` + all.join('')
}

async function checkUpdates(statusFn) {
  // Проверка идёт в фоне на бэкенде (скрейп 38 фиклов > nginx-таймаута → раньше
  // ловили 504). Стартуем и поллим статус вместо одного долгого запроса.
  statusFn('Запускаю проверку обновлений…')
  try {
    await api.post('/api/monitored/check', {})
  } catch (err) { statusFn('Ошибка запуска: ' + err.message.slice(0, 140), true); return }

  const POLL_MS = 3000
  const DEADLINE = Date.now() + 5 * 60 * 1000  // клиентский кэп: 5 минут
  let prevUpdates = 0
  const poll = async () => {
    let st
    try { st = await api.get('/api/monitored/check/status') }
    catch (err) { statusFn('Ошибка опроса статуса: ' + err.message.slice(0, 120), true); return }

    if (st.status === 'running') {
      if (Date.now() > DEADLINE) {
        statusFn('Проверка всё ещё идёт в фоне — загляни позже, списки обновятся сами')
        loadMonitored(); loadLibrary()
        return
      }
      const p = st.progress || {}
      if (p.total > 0) {
        const site = p.current_site || '?'
        const title = (p.current_title || '…').slice(0, 45)
        statusFn(`${site} · ${title} · ${p.current}/${p.total}`)
      } else {
        statusFn('Проверяю обновления…')
      }
      // Нашли новые обновления с прошлого тика — перерисовываем список сразу
      const updNow = p.updates_found || 0
      if (updNow > prevUpdates) {
        prevUpdates = updNow
        loadMonitored(); loadLibrary()
      }
      setTimeout(poll, POLL_MS)
      return
    }
    if (st.status === 'error') {
      statusFn('Ошибка проверки: ' + String(st.error || '').slice(0, 140), true)
      loadMonitored(); loadLibrary()
      return
    }
    if (st.status === 'done') {
      const r = st.result || {}
      statusFn(`Проверено: ${r.checked ?? 0}, с обновлениями: ${r.with_updates ?? 0}, докачано: ${r.downloaded ?? 0}`)
      renderUpdateErrors(r)
      loadMonitored(); loadLibrary()
      return
    }
    // idle/неизвестно: бэкенд перезапускался во время проверки — поток умер, статус сброшен
    statusFn('Проверка не запущена или была прервана — попробуй ещё раз', true)
    loadMonitored(); loadLibrary()
  }
  setTimeout(poll, 1500)
}
$('#check-updates').addEventListener('click', () => checkUpdates(accStatus))
// Кнопка на главной: статус показываем в строке ingest-status.
$('#check-updates-main').addEventListener('click', () => checkUpdates((msg, err) => {
  const el = $('#ingest-status'); el.hidden = false; el.classList.toggle('error', !!err); el.textContent = msg
}))


// author.today: интерактивный вход с 2FA-кодом (форма только сохраняет креды,
// а реальный вход требует кода с почты — здесь двухстадийный флоу).
;(function initAtLogin() {
  const form = $('#account-form'); if (!form) return
  const wrap = document.createElement('div')
  wrap.style.cssText = 'margin-top:8px'
  wrap.innerHTML =
    '<button type="button" id="at-login-btn" class="btn-ghost">Войти в author.today (с кодом)</button>' +
    '<div id="at-code-row" hidden style="margin-top:6px;display:flex;gap:6px">' +
      '<input id="at-code" placeholder="Код из письма author.today" style="flex:1" />' +
      '<button type="button" id="at-code-btn" class="btn-ghost">Подтвердить</button>' +
    '</div>'
  form.parentNode.insertBefore(wrap, form.nextSibling)

  $('#at-login-btn').addEventListener('click', async () => {
    const username = $('#acc-user').value.trim(), password = $('#acc-pass').value
    if (!username || !password) { accStatus('Введите логин и пароль author.today', true); return }
    accStatus('author.today: вход, запрашиваю код…')
    try {
      const r = await api.post('/api/accounts/at-login/start', { username, password })
      if (r.status === 'logged_in') { $('#at-code-row').hidden = true; accStatus('author.today: вход выполнен ✓'); loadAccounts() }
      else if (r.status === 'code_sent') { $('#at-code-row').hidden = false; $('#at-code').focus(); accStatus('author.today: ' + (r.message || 'код отправлен на почту')) }
      else { accStatus('author.today: ' + (r.message || 'ошибка входа'), true) }
    } catch (err) { accStatus('author.today: ' + err.message.slice(0, 140), true) }
  })

  $('#at-code-btn').addEventListener('click', async () => {
    const code = $('#at-code').value.trim()
    if (!code) { accStatus('Введите код из письма', true); return }
    accStatus('author.today: проверяю код…')
    try {
      const r = await api.post('/api/accounts/at-login/code', { code })
      if (r.status === 'logged_in') { $('#at-code-row').hidden = true; $('#at-code').value = ''; accStatus('author.today: вход выполнен ✓, сессия сохранена'); loadAccounts() }
      else { accStatus('author.today: ' + (r.message || 'неверный код'), true) }
    } catch (err) { accStatus('author.today: ' + err.message.slice(0, 140), true) }
  })
})()
