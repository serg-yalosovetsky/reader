// Сколько высоты экрана съедает «хром» читалки, сколько остаётся тексту и
// какого размера цели реально принимают палец.
//
// Тач-цель меряется ХИТ-ТЕСТОМ (document.elementFromPoint по вертикали и
// горизонтали через центр), а не геометрией бокса и не размером псевдоэлемента.
// Разница между этими двумя способами — не мелочь: она однажды уже дала
// «44 px» в отчёте при фактических 34 px под пальцем, потому что невидимый
// ::before обрезался overflow:hidden родителя и перекрывался соседним слоем.
// Бокс говорит, что элемент НАРИСОВАН, хит-тест — что по нему МОЖНО ПОПАСТЬ.
import puppeteer from 'puppeteer-core'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'

const PORT = 8251, ROOT = process.argv[2]
const server = spawn('python', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1', '--directory', ROOT], { stdio: 'ignore' })
await sleep(1200)
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})

const TARGETS = ['back-btn', 'reader-title', 'toc-btn', 'search-btn', 'settings-btn',
                 'fs-btn', 'more-btn', 'prev-btn', 'progress-wrap', 'next-btn']

const rows = []
for (const [name, w, h] of [['портрет 412×915', 412, 915], ['ландшафт 915×412', 915, 412], ['узкий 360×740', 360, 740]]) {
  const page = await browser.newPage()
  await page.setViewport({ width: w, height: h, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
  await sleep(2000)
  const m = await page.evaluate((TARGETS) => {
    const reader = document.getElementById('reader')
    reader.hidden = false
    document.getElementById('library').hidden = true
    const t = document.getElementById('reader-title')
    if (t) t.textContent = '196. Цена пролитой крови. Часть вторая'
    const H = (el) => el ? Math.round(el.getBoundingClientRect().height) : 0

    // Хит-тест: сколько подряд идущих пикселей вокруг центра элемента реально
    // достаются ЕМУ (или его потомку), а не соседнему слою.
    const hit = (el) => {
      if (!el) return { h: 0, w: 0 }
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) return { h: 0, w: 0 }
      const cx = Math.round(r.left + r.width / 2), cy = Math.round(r.top + r.height / 2)
      const mine = (x, y) => {
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false
        const hitEl = document.elementFromPoint(x, y)
        return !!hitEl && (hitEl === el || el.contains(hitEl))
      }
      if (!mine(cx, cy)) return { h: 0, w: 0 }
      let up = 0, down = 0, left = 0, right = 0
      while (up < 60 && mine(cx, cy - up - 1)) up++
      while (down < 60 && mine(cx, cy + down + 1)) down++
      while (left < 60 && mine(cx - left - 1, cy)) left++
      while (right < 60 && mine(cx + right + 1, cy)) right++
      return { h: up + down + 1, w: left + right + 1 }
    }

    const targets = {}
    for (const id of TARGETS) {
      const el = document.getElementById(id)
      const box = el ? el.getBoundingClientRect() : null
      targets[id] = {
        visH: box ? Math.round(box.height) : 0,
        visW: box ? Math.round(box.width) : 0,
        ...hit(el),
      }
    }
    return {
      top: H(document.getElementById('reader-top')),
      bottom: H(document.getElementById('reader-bottom')),
      host: H(document.getElementById('view-host')),
      screen: window.innerHeight,
      trackVis: H(document.getElementById('progress-track')),
      titleShown: getComputedStyle(document.getElementById('reader-title')).display !== 'none',
      titleWidth: Math.round(document.getElementById('reader-title').getBoundingClientRect().width),
      outside: [...document.querySelectorAll('.reader-top-actions .icon-btn')].map(b => b.id),
      targets,
    }
  }, TARGETS)
  rows.push({ name, ...m, chrome: m.top + m.bottom, textPct: +(m.host / m.screen * 100).toFixed(1) })
  await page.close()
}

let fails = 0
for (const r of rows) {
  console.log(`\n${r.name}: верх ${r.top} + низ ${r.bottom} = хром ${r.chrome} px | тексту ${r.host} px (${r.textPct}%)`)
  console.log(`   трек ${r.trackVis} px, заголовок виден ${r.titleShown} шириной ${r.titleWidth} px, снаружи: ${r.outside.join(' ')}`)
  for (const [id, t] of Object.entries(r.targets)) {
    const bad = t.h < 44
    if (bad) fails++
    console.log(`   ${bad ? 'МАЛО' : ' ok '} ${id.padEnd(14)} визуал ${String(t.visW).padStart(3)}×${String(t.visH).padStart(2)}  тап ${String(t.w).padStart(3)}×${String(t.h).padStart(2)}`)
  }
}
console.log(fails ? `\nПРОВАЛ: ${fails} целей ниже 44 px по вертикали` : '\nвсе цели >= 44 px по вертикали')
await browser.close(); server.kill('SIGKILL')
process.exit(fails ? 1 : 0)
