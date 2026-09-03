// Поведение нового хрома: цикл заголовка, меню «Ещё», бейдж на ⋮, полоса
// загрузки, риски глав в покое, двойной тап по центру.
// Модуль хрома грузим напрямую (без бэкенда: книгу открыть нечем).
import puppeteer from 'puppeteer-core'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'
const PORT = 8252, ROOT = process.argv[2]
const server = spawn('python', ['-m','http.server', String(PORT), '--bind','127.0.0.1','--directory', ROOT], { stdio: 'ignore' })
await sleep(1200)
const browser = await puppeteer.launch({ executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new', args: ['--no-sandbox','--disable-gpu'] })
const page = await browser.newPage()
await page.setViewport({ width: 412, height: 915, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
const errs = []
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
await sleep(2500)

const out = await page.evaluate(async () => {
  const r = {}
  document.getElementById('reader').hidden = false
  document.getElementById('library').hidden = true
  const ch = await import('/js/chrome.js')

  // --- Цикл заголовка -----------------------------------------------------
  ch.setBookMeta({ title: 'Парадокс второго шанса', author: 'Иван Автор' })
  const t = document.getElementById('reader-title')
  r.afterOpen_noChapterYet = t.textContent            // главы ещё нет -> книга
  ch.setChapterTitle('196. Цена пролитой крови')
  r.defaultMode = t.dataset.mode
  r.defaultText = t.textContent                       // дефолт = глава
  const cycle = []
  for (let i = 0; i < 4; i++) { t.click(); cycle.push(t.dataset.mode) }
  r.cycle = cycle                                     // book, author, chapter, book
  // Липкость: relocate не должен сбрасывать выбранный режим
  t.click()                                           // -> author
  const modeBefore = t.dataset.mode
  ch.setChapterTitle('197. Другая глава')
  r.stickyAcrossRelocate = (modeBefore === t.dataset.mode) && t.dataset.mode === 'author'
  // Книга без автора: тап не должен давать пустой заголовок
  ch.setBookMeta({ title: 'Книга без автора', author: '' })
  ch.setChapterTitle('Глава 1')
  t.click(); t.click()
  r.noAuthor_modes = t.dataset.mode
  r.noAuthor_text = t.textContent
  // Новая книга сбрасывает цикл на «главу»
  ch.setBookMeta({ title: 'Третья книга', author: 'Автор' })
  ch.setChapterTitle('Пролог')
  r.resetOnNewBook = t.dataset.mode

  // --- Меню «Ещё» ---------------------------------------------------------
  document.getElementById('more-btn').click()
  r.menuOpens = !document.getElementById('more-panel').hidden &&
                !document.getElementById('panel-overlay').hidden
  r.menuItems = [...document.querySelectorAll('#more-panel .more-item')]
                  .filter(b => !b.hidden).map(b => b.id)
  document.getElementById('panel-overlay').click()
  r.menuCloses = document.getElementById('more-panel').hidden
  // Меню и оглавление взаимоисключающи (одна панель на экране)
  document.getElementById('more-btn').click()
  document.getElementById('toc-btn').click()
  r.exclusive = document.getElementById('more-panel').hidden &&
                !document.getElementById('toc-panel').hidden
  document.getElementById('panel-overlay').click()

  // --- Индикаторы ---------------------------------------------------------
  const more = document.getElementById('more-btn')
  ch.setMoreBadge('new')
  r.badgeNew = getComputedStyle(more, '::after').content !== 'none' && more.dataset.badge === 'new'
  ch.setMoreBadge('')
  r.badgeCleared = getComputedStyle(more, '::after').content === 'none'
  ch.setMoreActive('translate')
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()
  r.tintOn = more.dataset.active === 'translate'
  ch.setMoreActive('')
  r.tintOff = !more.dataset.active
  ch.showTopLoading(true)
  r.loaderShown = !document.getElementById('top-loading').hidden
  ch.showTopLoading(false)
  r.loaderHidden = document.getElementById('top-loading').hidden

  // --- Доступность: состояние читаемо текстом, а не только цветом ---------
  ch.setMoreBadge('new')
  r.a11y_badgeInLabel = /новые главы/i.test(more.getAttribute('aria-label') || '')
  ch.setMoreActive('translate')
  r.a11y_activeInLabel = /перевод включён/i.test(more.getAttribute('aria-label') || '')
  ch.setMoreBadge(''); ch.setMoreActive('')
  r.a11y_labelClean = more.getAttribute('aria-label') === 'Ещё'
  // Доступное имя заголовка содержит видимый текст (2.5.3) и называет действие.
  ch.setBookMeta({ title: 'Книга', author: 'Автор' })
  ch.setChapterTitle('Глава 7')
  const lbl = t.getAttribute('aria-label') || ''
  r.a11y_titleHasVisibleText = lbl.includes(t.textContent)
  r.a11y_titleNamesAction = /показать/i.test(lbl)
  // aria-expanded следует за панелью.
  document.getElementById('more-btn').click()
  r.a11y_expandedOpen = more.getAttribute('aria-expanded') === 'true'
  document.getElementById('panel-overlay').click()
  r.a11y_expandedClosed = more.getAttribute('aria-expanded') === 'false'

  // --- Риски глав в покое невидимы, при удержании видны -------------------
  const wrap = document.getElementById('progress-wrap')
  const ticks = document.getElementById('chapter-ticks')
  r.ticksIdleOpacity = getComputedStyle(ticks).opacity
  wrap.classList.add('armed')
  // opacity анимируется .15s — читаем ПОСЛЕ перехода, иначе замерим 0 у самого
  // начала анимации и решим, что риски не проявляются.
  await new Promise((res) => setTimeout(res, 300))
  r.ticksArmedOpacity = getComputedStyle(ticks).opacity
  wrap.classList.remove('armed')
  await new Promise((res) => setTimeout(res, 300))
  r.ticksIdleAgain = getComputedStyle(ticks).opacity
  return r
})

// --- Двойной тап по центру: реальные touch-события ------------------------
// Без бэкенда документа книги нет, поэтому проверяем путь через #view-host.
const fsCalls = await page.evaluate(() => {
  window.__fs = 0
  const de = document.documentElement
  de.requestFullscreen = () => { window.__fs++; return Promise.resolve() }
  return 0
})
const host = await page.$('#view-host')
const box = await host.boundingBox()
const cx = Math.round(box.x + box.width / 2), cy = Math.round(box.y + box.height / 2)
await page.touchscreen.tap(cx, cy)
await sleep(90)
await page.touchscreen.tap(cx, cy)
await sleep(200)
const afterDouble = await page.evaluate(() => window.__fs)
// Одиночный тап (после паузы) не должен ничего включать
await sleep(600)
await page.touchscreen.tap(cx, cy)
await sleep(300)
const afterSingle = await page.evaluate(() => window.__fs)
// Двойной тап по КРАЮ (зона перелистывания) — не наш жест
const ex = Math.round(box.x + box.width * 0.04)
await page.touchscreen.tap(ex, cy); await sleep(90)
await page.touchscreen.tap(ex, cy); await sleep(200)
const afterEdge = await page.evaluate(() => window.__fs)
// Двойной тап при активном выделении текста — отдаём выделению
await page.evaluate(() => {
  const p = document.createElement('p')
  p.textContent = 'текст для выделения'
  p.style.cssText = 'position:absolute;left:40%;top:40%'
  document.getElementById('view-host').append(p)
  const rng = document.createRange(); rng.selectNodeContents(p)
  getSelection().removeAllRanges(); getSelection().addRange(rng)
})
await page.touchscreen.tap(cx, cy); await sleep(90)
await page.touchscreen.tap(cx, cy); await sleep(200)
const afterSelection = await page.evaluate(() => window.__fs)

console.log(JSON.stringify(out, null, 1))
console.log('двойной тап центр -> fullscreen вызовов:', afterDouble, '(ожидается 1)')
console.log('одиночный тап -> всего:', afterSingle, '(ожидается 1, т.е. не добавил)')
console.log('двойной тап по краю -> всего:', afterEdge, '(ожидается 1, т.е. не добавил)')
console.log('двойной тап при выделении -> всего:', afterSelection, '(ожидается 1, т.е. не добавил)')
console.log('ошибки страницы:', errs.length ? errs : 'нет')
await browser.close(); server.kill('SIGKILL')
