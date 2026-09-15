// Выбор темы в панели «Вид» (serg/tasks#912): раскладка, тач-цели, имена.
//
// Запуск (на VPS, мимо SSO): node theme-swatches.mjs http://127.0.0.1:8123
// Chrome — из CHROME_PATH. Код выхода ненулевой, если хоть одна проверка упала.
//
// Тач-цель меряется ХИТ-ТЕСТОМ (elementFromPoint по обеим осям от центра), а не
// боксом: у свотчей зона набиралась невидимым свесом ::before, свесы соседей
// перекрывались при gap 8px, и под пальцем было 37–39px вместо заявленных 44.
import puppeteer from 'puppeteer-core'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = process.argv[2] || 'http://127.0.0.1:8123'
const VIEWPORTS = [
  ['портрет 360×740', 360, 740], ['портрет 390×844', 390, 844],
  ['портрет 412×915', 412, 915], ['ландшафт 915×412', 915, 412],
]
const NAMES = {
  day: 'День', sepia: 'Сепия', grey: 'Серая', dusk: 'Сумерки',
  night: 'Ночь', terminal: 'Терминал', black: 'Чёрная', phosphor: 'Фосфор',
}

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const results = []
const check = (name, ok, detail) => results.push({ name, ok, ...detail })

for (const [label, w, h] of VIEWPORTS) {
  const page = await browser.newPage()
  await page.setViewport({ width: w, height: h, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 40000 })
  await sleep(2000)
  const m = await page.evaluate((names) => {
    // Свотчи живут внутри #reader — без этого геометрия нулевая.
    document.getElementById('reader').hidden = false
    document.getElementById('library').hidden = true
    document.getElementById('settings-panel').hidden = false
    const list = [...document.querySelectorAll('.swatch')]
    // Выбор должен быть ровно один и заметен не только цветом.
    list.find((b) => b.dataset.theme === 'phosphor')?.click()

    // Сколько подряд идущих пикселей от центра достаются самому элементу.
    const hit = (el) => {
      const r = el.getBoundingClientRect()
      const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2)
      const mine = (x, y) => { const e = document.elementFromPoint(x, y); return !!e && (e === el || el.contains(e)) }
      if (!mine(cx, cy)) return { hw: 0, hh: 0 }
      const grow = (dx, dy) => { let n = 0; while (n < 60 && mine(cx + dx * (n + 1), cy + dy * (n + 1))) n++; return n }
      return { hw: grow(-1, 0) + grow(1, 0) + 1, hh: grow(0, -1) + grow(0, 1) + 1 }
    }

    const rows = {}
    const swatches = list.map((b) => {
      const r = b.getBoundingClientRect()
      const top = Math.round(r.top)
      rows[top] = (rows[top] || 0) + 1
      const after = getComputedStyle(b, '::after').content
      return {
        theme: b.dataset.theme, top, box: [Math.round(r.width), Math.round(r.height)],
        ...hit(b), label: b.getAttribute('aria-label') || b.title || '',
        text: (b.textContent || '').trim(),
        current: b.getAttribute('aria-current') === 'true',
        badge: after && after !== 'none' && after !== '""' && after !== 'normal',
      }
    })
    const otherRows = [...document.querySelectorAll('.setting-row')]
      .filter((r) => !r.querySelector('.theme-swatches'))
      .map((r) => getComputedStyle(r).flexDirection)
    return { lines: Object.keys(rows).sort((a, b) => a - b).map((k) => rows[k]), swatches, otherRows, names }
  }, NAMES)

  check(`${label}: свотчи без сироты (4+4)`,
    m.lines.length === 2 && m.lines.every((n) => n === 4), { lines: m.lines })

  const small = m.swatches.filter((s) => s.hw < 44 || s.hh < 44)
  check(`${label}: тач-цель каждого свотча ≥44px по хит-тесту`, small.length === 0,
    { worst: m.swatches.map((s) => `${s.theme} ${s.hw}×${s.hh}`).join(', ') })

  const badName = m.swatches.filter((s) => s.label !== `Тема: ${NAMES[s.theme]}`)
  check(`${label}: у свотча доступное имя темы`, badName.length === 0,
    { bad: badName.map((s) => `${s.theme}: «${s.label}» текст «${s.text}»`) })

  const cur = m.swatches.filter((s) => s.current)
  check(`${label}: выбранный отмечен не только цветом`, cur.length === 1 && cur[0].badge,
    { current: cur.map((s) => `${s.theme} badge=${s.badge}`) })

  check(`${label}: остальные строки панели остались горизонтальными`,
    m.otherRows.length >= 4 && m.otherRows.every((d) => d === 'row'), { otherRows: m.otherRows })

  await page.close()
}

await browser.close()
for (const r of results) console.log(JSON.stringify(r))
const failed = results.filter((r) => !r.ok)
console.log(failed.length ? `FAIL: ${failed.length} из ${results.length}` : `ok: ${results.length} из ${results.length}`)
process.exit(failed.length ? 1 : 0)
