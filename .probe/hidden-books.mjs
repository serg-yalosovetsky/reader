// Скрытые книги в браузере (serg/tasks#923): кнопка, сетка библиотеки, поиск.
//
// Запуск (на VPS, мимо SSO): node hidden-books.mjs http://127.0.0.1:8123
// Chrome — из CHROME_PATH. Код выхода ненулевой, если хоть одна проверка упала.
//
// Стенд работает с ЖИВОЙ библиотекой: скрывает настоящую книгу и в конце
// ОБЯЗАТЕЛЬНО возвращает её обратно (в том числе при падении проверок).
import puppeteer from 'puppeteer-core'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = process.argv[2] || 'http://127.0.0.1:8123'
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
await page.setViewport({ width: 412, height: 915, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
const errs = []
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()) })

const results = []
const check = (name, ok, detail) => results.push({ name, ok, ...detail })
let victim = null

try {
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 40000 })
  await sleep(4000)

  // Берём книгу с самым редким названием из первых карточек — чтобы поиск по
  // её названию не тонул в сотне однофамильцев.
  victim = await page.evaluate(() => {
    const card = document.querySelector('#book-grid .book-card')
    return card && card._w ? { id: card._w.id, title: card._w.title } : null
  })
  check('библиотека отрисовалась', !!victim, { victim })

  const before = await page.evaluate(() => document.querySelectorAll('#book-grid .book-card').length)

  // Скрываем через настоящую кнопку на странице книги, а не запросом.
  const opened = await page.evaluate(async () => {
    const card = document.querySelector('#book-grid .book-card')
    card.click()
    return new Promise((r) => setTimeout(() => r(!document.getElementById('book-page').hidden), 1500))
  })
  check('страница книги открылась', opened, {})
  const btnText = await page.evaluate(() => document.getElementById('bp-hide')?.textContent?.trim() || null)
  check('на странице книги есть кнопка скрытия', /Скрыть/.test(btnText || ''), { btnText })

  await page.evaluate(() => document.getElementById('bp-hide').click())
  await sleep(1500)
  await page.evaluate(() => history.back())
  await sleep(1500)

  const after = await page.evaluate(() => document.querySelectorAll('#book-grid .book-card').length)
  check('скрытая книга ушла из сетки библиотеки', after === before - 1, { before, after })

  // Поиск по названию: книга обязана найтись и быть помечена.
  const found = await page.evaluate(async (title) => {
    const inp = document.getElementById('lib-q')
    inp.value = title
    inp.dispatchEvent(new Event('input', { bubbles: true }))
    await new Promise((r) => setTimeout(r, 2500))
    const cards = [...document.querySelectorAll('#book-grid .book-card')]
    const mine = cards.find((c) => c._w && c._w.title === title)
    return {
      total: cards.length,
      present: !!mine,
      marked: !!mine && mine.classList.contains('is-hidden') && !!mine.querySelector('.hidden-badge'),
    }
  }, victim.title)
  check('скрытая книга находится поиском и помечена', found.present && found.marked, found)
} finally {
  // Вернуть книгу в библиотеку в любом случае — стенд не должен оставлять
  // живую библиотеку в изменённом состоянии.
  if (victim) {
    const restored = await page.evaluate(async (id) => {
      const r = await fetch(`/api/library/${id}/hidden`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: false }),
      })
      return r.status
    }, victim.id).catch(() => 0)
    check('книга возвращена в библиотеку', restored === 200, { restored })
  }
  await browser.close()
}

for (const r of results) console.log(JSON.stringify(r))
if (errs.length) console.log('ошибки страницы:', JSON.stringify(errs.slice(0, 5)))
const failed = results.filter((r) => !r.ok)
console.log(failed.length ? `FAIL: ${failed.length} из ${results.length}` : `ok: ${results.length} из ${results.length}`)
process.exit(failed.length ? 1 : 0)
