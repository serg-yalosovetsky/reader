// Утилита визуальной проверки читалки в реальном браузере (Edge/Chrome) через
// puppeteer-core. Headless --screenshot не дожидается раскладки iframe пагинатора,
// поэтому используем ожидание.
//
// Запуск (нужен запущенный сервер и установленный Edge):
//   $env:EDGE_PATH="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
//   node scripts/shoot.mjs "http://127.0.0.1:8123/?open=1" out.png [theme]
import puppeteer from 'puppeteer-core'

const EDGE = process.env.EDGE_PATH
const url = process.argv[2] || 'http://127.0.0.1:8123/'
const out = process.argv[3] || 'shot.png'
const theme = process.argv[4]

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1100, height: 800 })
page.on('console', (m) => console.log('PAGE>', m.text()))
page.on('pageerror', (e) => console.log('PAGEERR>', e.message))

if (theme) {
  await page.evaluateOnNewDocument((t) => {
    localStorage.setItem('reader.prefs', JSON.stringify(
      { theme: t, fontScale: 1, marginLevel: 1, fontFamily: 'serif', flow: 'paginated' }))
  }, theme)
}

await page.goto(url, { waitUntil: 'networkidle0', timeout: 30000 })
await new Promise((r) => setTimeout(r, 4000)) // дать пагинатору разложить iframe
await page.screenshot({ path: out })
await browser.close()
console.log('saved', out)
