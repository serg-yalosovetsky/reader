// Тема «Фосфор» на ЖИВОМ сервере (serg/tasks#911): свотч, сохранение выбора,
// цикл кнопки темы в библиотеке, неизменность остальных тем.
//
// Запуск (на VPS, мимо SSO): node theme-phosphor.mjs http://127.0.0.1:8123
// Chrome — из CHROME_PATH. Код выхода ненулевой, если хоть одна проверка упала.
import puppeteer from 'puppeteer-core'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = process.argv[2] || 'http://127.0.0.1:8123'
// Цвета палитр до правки (css/theme.css на e80afc7): у старых тем меняться нечему.
const BEFORE = {
  day: ['#ffffff', '#1f2227'], sepia: ['#f5ecd9', '#5b4636'], grey: ['#cfd2d4', '#23262a'],
  dusk: ['#2b2e33', '#dde1e7'], night: ['#15171a', '#cbd0d6'], terminal: ['#0d1117', '#3fb950'],
  black: ['#000000', '#bdbdbd'],
}
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
await page.setViewport({ width: 412, height: 915, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
const errs = []
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message))
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 40000 })
await sleep(2500)

const results = []
const check = (name, ok, detail) => results.push({ name, ok, ...detail })
const snap = () => page.evaluate(() => {
  const cs = getComputedStyle(document.documentElement)
  const body = getComputedStyle(document.body)
  return {
    theme: document.documentElement.dataset.theme,
    bg: cs.getPropertyValue('--bg').trim(), fg: cs.getPropertyValue('--fg').trim(),
    scheme: cs.colorScheme, bodyBg: body.backgroundColor, bodyColor: body.color,
  }
})

// 1. Свотч есть и выбирает тему.
const sw = await page.evaluate(() => {
  const b = document.querySelector('.swatch[data-theme="phosphor"]')
  if (!b) return null
  document.getElementById('settings-panel').hidden = false
  b.click()
  return { title: b.title, current: b.getAttribute('aria-current') }
})
const s1 = await snap()
check('свотч «Фосфор» выбирает тему', !!sw && s1.theme === 'phosphor' && s1.bg === '#000000' && s1.fg === '#39d353'
  && s1.scheme === 'dark' && s1.bodyBg === 'rgb(0, 0, 0)' && s1.bodyColor === 'rgb(57, 211, 83)',
  { swatch: sw, ...s1 })

// 2. Выбор переживает перезагрузку.
await page.reload({ waitUntil: 'domcontentloaded' })
await sleep(2500)
const s2 = await snap()
check('выбор сохраняется после перезагрузки', s2.theme === 'phosphor' && s2.bodyBg === 'rgb(0, 0, 0)', s2)

// 3. Кнопка темы в библиотеке проходит через «Фосфор».
const cycle = await page.evaluate(() => {
  const btn = document.querySelector('#lib-theme-btn')
  if (!btn) return null
  const seen = []
  for (let i = 0; i < 9; i++) { btn.click(); seen.push(document.documentElement.dataset.theme) }
  return seen
})
check('кнопка темы библиотеки проходит все 8 тем', !!cycle && new Set(cycle).size === 8 && cycle.includes('phosphor'), { cycle })

// 4. Остальные темы не изменились.
const others = await page.evaluate((names) => names.map((n) => {
  document.documentElement.dataset.theme = n
  const cs = getComputedStyle(document.documentElement)
  return [n, cs.getPropertyValue('--bg').trim(), cs.getPropertyValue('--fg').trim()]
}), Object.keys(BEFORE))
const drift = others.filter(([n, bg, fg]) => bg !== BEFORE[n][0] || fg !== BEFORE[n][1])
check('остальные темы не изменились', drift.length === 0, { drift })

// Вернуть браузерный профиль в исходное (он временный, но на всякий случай).
await page.evaluate(() => { try { localStorage.clear() } catch {} })
await browser.close()
for (const r of results) console.log(JSON.stringify(r))
if (errs.length) console.log('ошибки страницы:', JSON.stringify(errs))
const failed = results.filter((r) => !r.ok)
console.log(failed.length ? `FAIL: ${failed.length} из ${results.length}` : `ok: ${results.length} из ${results.length}`)
process.exit(failed.length ? 1 : 0)
