// Панель «Скачивания» (serg/tasks#893): что качается сейчас и что закончилось
// за сутки. Данные — с сервера (таблица ingest_job, #892), поэтому панель
// переживает перезагрузку страницы и рестарт сервиса и видна со второго
// устройства. Локально помним только одно: свернул ли её человек.
import { $, escapeHtml } from './core/dom.js'
import { api } from './core/api.js'

const POLL_MS = 3000
const COLLAPSE_KEY = 'reader:dlCollapsed'
const UNIT = { chapters: 'гл.', pages: 'стр.' }

export function formatElapsed(sec) {
  if (sec == null || !Number.isFinite(sec)) return ''
  const s = Math.max(0, Math.round(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const mm = h ? String(m).padStart(2, '0') : String(m)
  return (h ? `${h}:` : '') + `${mm}:${String(r).padStart(2, '0')}`
}

// Ссылка без схемы и без логина/пароля — запасное имя, пока название книги ещё
// не известно (сервер и так маскирует userinfo, здесь — второй рубеж).
function shortQuery(q) {
  return String(q || '').replace(/^\w+:\/\/(?:[^/@\s]*@)?/, '').replace(/\/$/, '')
}

export function describeJob(job) {
  const p = job.progress || {}
  const title = job.title || (job.work && job.work.title) || shortQuery(job.query) || 'Книга'
  const parts = []
  let pct = null
  if (job.status === 'queued') {
    parts.push('в очереди')
    if (job.interrupted_by) parts.push('продолжится после рестарта')
  } else if (job.status === 'running') {
    if (p.stage) parts.push(p.stage)
    if (p.total) {
      pct = Math.min(100, Math.round((p.done / p.total) * 100))
      parts.push(`${p.done} из ${p.total} ${UNIT[p.unit] || ''}`.trim())
    } else if (p.unit === 'requests' && p.done) {
      parts.push(`запросов: ${p.done}`)
    }
    if (job.interruptions) parts.push('возобновлено после рестарта')
    const t = formatElapsed(job.elapsed_s)
    if (t) parts.push(t)
  } else if (job.status === 'done') {
    parts.push('готово')
    const ch = job.work && job.work.chapters
    if (ch) parts.push(`${ch} гл.`)
    const t = formatElapsed(job.elapsed_s)
    if (t) parts.push(t)
  } else if (job.status === 'error') {
    parts.push(`ошибка: ${String(job.error || 'не удалось скачать').slice(0, 200)}`)
  }
  return { title, sub: parts.join(' · '), pct, kind: job.status }
}

export function countActive(jobs) {
  return (jobs || []).filter((j) => j.status === 'queued' || j.status === 'running').length
}

export function renderJobsHtml(jobs) {
  return (jobs || []).map((job) => {
    const d = describeJob(job)
    const bar = d.pct == null
      ? ''
      : `<div class="dl-bar" aria-hidden="true"><i style="width:${d.pct}%"></i></div>`
    return `<li class="dl-item dl-${escapeHtml(d.kind)}">`
      + `<div class="dl-title" title="${escapeHtml(d.title)}">${escapeHtml(d.title)}</div>`
      + `<div class="dl-sub">${escapeHtml(d.sub)}</div>${bar}</li>`
  }).join('')
}

// ---------------- состояние панели ----------------
let timer = null
let active = 0
let lastData = { active: 0, jobs: [] }
let userOpened = false
let finishedListener = null
const ownJobs = new Set()
const prevStatus = new Map()

export function onDownloadFinished(fn) { finishedListener = fn }

function isCollapsed() {
  try { return localStorage.getItem(COLLAPSE_KEY) === '1' } catch { return false }
}
function setCollapsed(v) {
  // Приватный режим/запрет хранилища: свёрнутость просто не запомнится между
  // перезагрузками — панель от этого не ломается.
  try { localStorage.setItem(COLLAPSE_KEY, v ? '1' : '0') } catch { /* см. выше */ }
}

function render(data) {
  lastData = data
  const jobs = data.jobs || []
  active = typeof data.active === 'number' ? data.active : countActive(jobs)
  const toggle = $('#dl-toggle')
  const panel = $('#dl-panel')
  const list = $('#dl-list')
  if (!toggle || !panel || !list) return
  const hasJobs = jobs.length > 0
  toggle.hidden = !hasJobs
  toggle.classList.toggle('is-active', active > 0)
  toggle.textContent = active > 0 ? `⬇ ${active}` : '⬇'
  toggle.title = active > 0 ? `Скачивается: ${active}` : 'Скачивания за сутки'
  // Сама раскрывается только пока что-то качается: законченные за сутки задания
  // не должны выпрыгивать при каждом открытии библиотеки.
  const open = hasJobs && !isCollapsed() && (active > 0 || userOpened)
  panel.hidden = !open
  toggle.setAttribute('aria-expanded', String(open))
  const count = $('#dl-count')
  if (count) count.textContent = active ? `· ${active}` : ''
  list.innerHTML = renderJobsHtml(jobs)
  for (const j of jobs) {
    const was = prevStatus.get(j.job_id)
    if (was && was !== 'done' && j.status === 'done' && !ownJobs.has(j.job_id) && finishedListener) {
      finishedListener(j)
    }
    prevStatus.set(j.job_id, j.status)
  }
}

function schedule(ms) {
  clearTimeout(timer)
  timer = setTimeout(tick, ms)
}

async function tick() {
  timer = null
  if (document.visibilityState !== 'visible') return // вернёмся по visibilitychange
  let data
  try {
    data = await api.get('/api/ingest/jobs?limit=20')
  } catch (err) {
    // Протухшая сессия: баннер перелогина покажет api.js, молотить незачем.
    if (err && err.name === 'AuthRequiredError') return
    // 404 в окне деплоя (фронт новее бэкенда) или обрыв связи — «данных нет»,
    // но пока знаем об активных заданиях, спросим ещё, реже.
    if (active > 0) schedule(POLL_MS * 3)
    return
  }
  render(data)
  if (active > 0) schedule(POLL_MS)
}

export function refreshDownloads() { schedule(0) }

// Скачивание, начатое в этой вкладке: панель раскрывается, опрос начинается сразу.
export function watchDownloads(jobId) {
  if (jobId) ownJobs.add(jobId)
  setCollapsed(false)
  schedule(300)
}

function init() {
  $('#dl-toggle')?.addEventListener('click', () => {
    const panel = $('#dl-panel')
    if (panel && panel.hidden) {
      userOpened = true
      setCollapsed(false)
    } else {
      userOpened = false
      setCollapsed(true)
    }
    render(lastData)
  })
  $('#dl-close')?.addEventListener('click', () => {
    userOpened = false
    setCollapsed(true)
    render(lastData)
  })
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') schedule(0)
  })
  refreshDownloads()
}

if (typeof document !== 'undefined') init()
