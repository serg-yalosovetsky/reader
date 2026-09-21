// Ручная обложка в браузере (serg/tasks#1055): обе кнопки, предпросмотр, отмена.
//
// Запуск (на VPS, мимо SSO): CHROME_PATH=... node manual-cover.mjs http://127.0.0.1:8123
// Код выхода ненулевой, если хоть одна проверка упала.
//
// Стенд работает с ЖИВОЙ библиотекой, но ТОЛЬКО с пробной книгой «ZZ probe 1055 UI»:
// заводит её сам и в конце удаляет (в том числе при падении проверок). Реальные
// книги читаются — как источник копируемой обложки, — но не меняются.
import puppeteer from 'puppeteer-core'
import fs from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = process.argv[2] || 'http://127.0.0.1:8123'
const TITLE = 'ZZ probe 1055 UI'
const PNG = process.env.PROBE_PNG   // кладёт вызывающий: 300x450 PNG, ярко-красный

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH,
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
const errs = []
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message))
page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errs.push('console: ' + m.text()) })

const results = []
const check = (name, ok, detail) => results.push({ ...detail, name, ok: !!ok })  // ok — последним: деталь не должна его затирать
let probeId = null

const coverInfo = () => page.evaluate(() => {
  const img = document.querySelector('.bp-cover img')
  return img ? { src: img.getAttribute('src'), w: img.naturalWidth, complete: img.complete } : null
})
const modalOpen = () => page.evaluate(() => !!document.querySelector('.cp-overlay'))
const clickText = (sel, re) => page.evaluate((s, r) => {
  const el = [...document.querySelectorAll(s)].find((e) => new RegExp(r).test(e.textContent))
  if (el) el.click()
  return !!el
}, sel, re.source)

try {
  await page.setViewport({ width: 412, height: 915, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 40000 })
  // Пробная книга: уникальный fb2 (без обложки внутри).
  const fb2 = `<?xml version="1.0" encoding="UTF-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info><genre>sf</genre><author><first-name>Probe</first-name><last-name>Author</last-name></author><book-title>${TITLE}</book-title></title-info></description><body><section><p>probe ${Date.now()}</p></section></body></FictionBook>`
  probeId = await page.evaluate(async (fb2, name) => {
    const fd = new FormData()
    fd.append('file', new File([fb2], name, { type: 'application/x-fictionbook+xml' }))
    const r = await fetch('/api/library/upload', { method: 'POST', body: fd })
    return r.ok ? (await r.json()).id : null
  }, fb2, 'probe-1055.fb2')
  check('пробная книга заведена, название из файла', !!probeId, { probeId })

  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 40000 })
  await sleep(4000)
  const opened = await page.evaluate(async (title) => {
    const card = [...document.querySelectorAll('#book-grid .book-card')].find((c) => c._w && c._w.title === title)
    if (!card) return { found: false }
    card.click()
    await new Promise((r) => setTimeout(r, 1500))
    return { found: true, open: !document.getElementById('book-page').hidden }
  }, TITLE)
  check('пробная книга в сетке, страница открылась', opened.found && opened.open, opened)

  const btns = await page.evaluate(() => ({
    up: document.getElementById('bp-uploadcover')?.textContent?.trim() || null,
    copy: document.getElementById('bp-copycover')?.textContent?.trim() || null,
  }))
  check('обе кнопки на странице книги', /Загрузить обложку/.test(btns.up || '') && /Взять обложку из другой книги/.test(btns.copy || ''), btns)

  // --- загрузка файла: отмена ничего не меняет --------------------------------------
  const before = await coverInfo()
  const [chooser1] = await Promise.all([page.waitForFileChooser(), page.click('#bp-uploadcover')])
  await chooser1.accept([PNG])
  await sleep(600)
  check('после выбора файла открылось окно предпросмотра', await modalOpen(), {})
  const geo = await page.evaluate(() => {
    const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return { w: Math.round(b.width), h: Math.round(b.height) } }
    return {
      okBtn: r('[data-cp=ok]'), cancel: r('[data-cp=cancel]'), close: r('.cp-x'),
      scrollX: document.documentElement.scrollWidth - window.innerWidth,
      previewLoaded: [...document.querySelectorAll('.cp-modal img')].some((i) => i.naturalWidth > 0),
    }
  })
  check('кнопки окна ≥44 px и нет горизонтального скролла', geo.okBtn.h >= 44 && geo.cancel.h >= 44 && geo.close.h >= 44 && geo.close.w >= 44 && geo.scrollX <= 0, geo)
  check('предпросмотр показывает выбранную картинку', geo.previewLoaded, {})
  await page.click('[data-cp=cancel]')
  await sleep(400)
  const afterCancel = await coverInfo()
  check('отмена: окно закрыто, обложка прежняя', !(await modalOpen()) && JSON.stringify(afterCancel) === JSON.stringify(before), { before, afterCancel })

  // Esc тоже закрывает и не оставляет зависшего ожидания
  const [chooserEsc] = await Promise.all([page.waitForFileChooser(), page.click('#bp-uploadcover')])
  await chooserEsc.accept([PNG])
  await sleep(500)
  await page.keyboard.press('Escape')
  await sleep(400)
  check('Esc закрывает окно', !(await modalOpen()), {})

  // --- загрузка файла: подтверждение ---------------------------------------------------
  const [chooser2] = await Promise.all([page.waitForFileChooser(), page.click('#bp-uploadcover')])
  await chooser2.accept([PNG])
  await sleep(500)
  await page.click('[data-cp=ok]')
  await sleep(2500)
  const up = await coverInfo()
  check('после «Заменить» на странице новая обложка (cover_v в URL, картинка загрузилась)', up && up.w > 0 && /v=\d{6,}/.test(up.src) && up.src !== before?.src, { before, up })
  // --- взять у другой книги ------------------------------------------------------------
  await page.click('#bp-copycover')
  await sleep(800)
  const pick = await page.evaluate(() => ({
    open: !!document.querySelector('.cp-overlay'),
    books: document.querySelectorAll('.cp-book').length,
    self: [...document.querySelectorAll('.cp-btitle')].some((e) => e.textContent.includes('ZZ probe 1055 UI')),
    scrollX: document.documentElement.scrollWidth - window.innerWidth,
  }))
  check('окно выбора книги: есть книги, себя нет, нет горизонтального скролла', pick.open && pick.books > 3 && !pick.self && pick.scrollX <= 0, pick)

  await page.type('.cp-search', 'ring')
  await sleep(700)
  const found = await page.evaluate(() => [...document.querySelectorAll('.cp-book')].map((b) => `${b.querySelector('.cp-btitle')?.textContent || ''} ${b.querySelector('.cp-bauthor')?.textContent || ''}`))
  check('поиск сужает список: в каждой найденной есть «ring» в названии или авторе', found.length > 0 && found.every((t) => /ring/i.test(t)) && found.length < pick.books, { shown: found.length, of: pick.books, sample: found.slice(0, 3) })
  await page.evaluate(() => { document.querySelector('.cp-search').value = ''; document.querySelector('.cp-search').dispatchEvent(new Event('input', { bubbles: true })) })
  await sleep(800)

  const cardGeo = await page.evaluate(() => {
    const b = document.querySelector('.cp-book:not(.cp-nocover)')
    if (!b) return null
    const r = b.getBoundingClientRect()
    return { h: Math.round(r.height), w: Math.round(r.width) }
  })
  check('карточка книги в выборе — тач-цель ≥44 px', cardGeo && cardGeo.h >= 44 && cardGeo.w >= 44, cardGeo)

  // выбрать книгу с обложкой (не помеченную .cp-nocover), подтвердить
  const chosen = await page.evaluate(() => {
    const b = [...document.querySelectorAll('.cp-book')].find((x) => !x.classList.contains('cp-nocover') && x.querySelector('img'))
    if (!b) return null
    const t = b.querySelector('.cp-btitle').textContent
    b.click()
    return t
  })
  await sleep(700)
  const confirm = await page.evaluate(() => ({ ok: !!document.querySelector('[data-cp=ok]'), note: document.querySelector('.cp-note')?.textContent || '' }))
  check('выбор книги ведёт к предпросмотру с пометкой «та книга не изменится»', chosen && confirm.ok && /не изменится/.test(confirm.note), { chosen, note: confirm.note })
  const mid = await coverInfo()
  await page.click('[data-cp=ok]')
  await sleep(2500)
  const copied = await coverInfo()
  check('после копирования обложка на странице сменилась', copied && copied.w > 0 && copied.src !== mid.src && /v=\d{6,}/.test(copied.src), { mid, copied })

  // --- сетка библиотеки показывает новую обложку без перезагрузки -----------------------
  await page.evaluate(() => history.back())
  await sleep(1500)
  const inGrid = await page.evaluate((title) => {
    const card = [...document.querySelectorAll('#book-grid .book-card')].find((c) => c._w && c._w.title === title)
    return card ? { v: card._w.cover_v, img: card.querySelector('img')?.getAttribute('src') || null } : null
  }, TITLE)
  const serverV = await page.evaluate(async (id) => (await (await fetch('/api/library')).json()).find((w) => w.id === id)?.cover_v, probeId)
  check('cover_v карточки в сетке = серверному (без перезагрузки страницы)', inGrid && inGrid.v === serverV && (!inGrid.img || inGrid.img.includes(`v=${serverV}`)), { inGrid, serverV })

  // --- узкий экран 360 px ---------------------------------------------------------------
  await page.setViewport({ width: 360, height: 740, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  await page.evaluate((title) => {
    [...document.querySelectorAll('#book-grid .book-card')].find((c) => c._w && c._w.title === title)?.click()
  }, TITLE)
  await sleep(1500)
  await page.click('#bp-copycover')
  await sleep(800)
  const narrow = await page.evaluate(() => ({ scrollX: document.documentElement.scrollWidth - window.innerWidth, cols: getComputedStyle(document.querySelector('.cp-grid')).gridTemplateColumns.split(' ').length, modalW: Math.round(document.querySelector('.cp-modal').getBoundingClientRect().width) }))
  check('360 px: окно выбора без горизонтального скролла', narrow.scrollX <= 0 && narrow.modalW <= 360, narrow)
  await page.keyboard.press('Escape')
  await sleep(300)
} finally {
  if (probeId) {
    const del = await page.evaluate(async (id) => (await fetch(`/api/library/${id}`, { method: 'DELETE' })).status, probeId).catch(() => 0)
    check('пробная книга удалена', del === 200, { del })
  }
  await browser.close()
}

for (const r of results) console.log(JSON.stringify(r))
if (errs.length) console.log('ошибки страницы:', JSON.stringify(errs.slice(0, 6)))
const failed = results.filter((r) => !r.ok)
console.log(failed.length ? `FAIL: ${failed.length} из ${results.length}` : `ok: ${results.length} из ${results.length}`)
process.exit(failed.length ? 1 : 0)
