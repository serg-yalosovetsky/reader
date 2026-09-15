// Откат прокрутки в «Ленте» после отпускания пальца (serg/tasks#909).
//
// Жалоба: длинная глава, ведёшь пальцем вниз, текст идёт, отпускаешь — и
// оказываешься примерно на страницу выше. В paginator.js вид возвращается к
// якорю #anchor, когда меняется размер документа главы (onExpand) или высота
// контейнера; якорь же обновляется только отложенно, после прокрутки. Стенд
// проверяет, что рост документа НЕ отбрасывает читателя назад.
//
// Настоящий frontend/vendor/foliate-js/paginator.js, настоящие касания (CDP
// Input.dispatchTouchEvent), глава — синтетическая. Рост документа — это то, что
// в жизни делают ленивые картинки из core/inline-images.js, шрифты и перевод.
//
// Сценарии 1–3 доказывают дефект: на коде до правки (305128d) они красные —
// отпустили палец на 452, итог 366; каждый рост документа сдвигал текст на
// 23–24 px. Сценарии 4–7 — регрессии: возврат к якорю, ради которого он и
// существует, обязан работать и до, и после правки.
//
// Запуск: cd .probe && npm install && node scroll-anchor.mjs ../frontend
// Chrome — из CHROME_PATH. Код выхода ненулевой, если хоть один сценарий упал.
import puppeteer from 'puppeteer-core'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'

const PORT = 8253, ROOT = process.argv[2] || '../frontend'
const PY = process.platform === 'win32' ? 'python' : 'python3'
const server = spawn(PY, ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1', '--directory', ROOT], { stdio: 'ignore' })
await sleep(1200)
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})

// Страница-носитель на том же origin, что и paginator.js: сам файл отдаёт
// http.server, а пустую страницу подставляем перехватом запроса.
async function openStand() {
  const page = await browser.newPage()
  await page.setViewport({ width: 412, height: 915, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  const errs = []
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message))
  await page.setRequestInterception(true)
  page.on('request', (r) => {
    if (r.url().endsWith('/__stand.html')) {
      return r.respond({ status: 200, contentType: 'text/html; charset=utf-8',
        body: '<!doctype html><meta charset="utf-8"><body style="margin:0"></body>' })
    }
    r.continue()
  })
  await page.goto(`http://127.0.0.1:${PORT}/__stand.html`, { waitUntil: 'domcontentloaded' })
  await page.evaluate(async () => {
    await import('/vendor/foliate-js/paginator.js')
    const p = document.createElement('foliate-paginator')
    p.style.cssText = 'position:fixed;inset:0'
    p.setAttribute('flow', 'scrolled')
    document.body.append(p)
    window.__relocs = []
    p.addEventListener('relocate', (e) => window.__relocs.push(e.detail.reason))
    const para = 'Текст длинной главы для проверки прокрутки в ленте. '.repeat(6)
    const body = Array.from({ length: 400 }, (_, i) => `<p>Абзац ${i}. ${para}</p>`).join('')
    const html = `<!doctype html><html><head><meta charset="utf-8"></head><body>${body}</body></html>`
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }))
    p.open({ dir: 'ltr', sections: [{ load: () => url, linear: 'yes' }] })
    await p.goTo({ index: 0 })
    window.__p = p
    // Документ главы вырос в конце (как дорисовавшаяся картинка ниже экрана).
    window.__grow = (px) => {
      const doc = p.getContents()[0].doc
      const d = doc.createElement('div')
      d.style.height = px + 'px'
      doc.body.append(d)
    }
    // Документ вырос ВЫШЕ экрана (картинка в уже пройденном тексте).
    window.__growAbove = (px) => {
      const doc = p.getContents()[0].doc
      const d = doc.createElement('div')
      d.style.height = px + 'px'
      doc.body.prepend(d)
    }
  })
  await sleep(1200)
  const cdp = await page.createCDPSession()
  return { page, cdp, errs }
}

const start = (page) => page.evaluate(() => Math.round(window.__p.start))

// Палец ведёт снизу вверх (текст едет вниз): from→to по Y за steps шагов.
async function swipe(cdp, { fromY, toY, steps, stepMs }) {
  const x = 206
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: fromY }] })
  for (let i = 1; i <= steps; i++) {
    await sleep(stepMs)
    const y = fromY + (toY - fromY) * i / steps
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y }] })
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
}
const LONG = { fromY: 700, toY: 250, steps: 30, stepMs: 16 }

const results = []
const check = (name, ok, detail) => { results.push({ name, ok, ...detail }) }

// --- 1. Свайп после открытия главы, потом документ подрос --------------------
// Позиция, до которой долистали, должна остаться. Откат к началу = дефект.
{
  const { page, cdp, errs } = await openStand()
  const s0 = await start(page)
  await swipe(cdp, LONG)
  await sleep(2500) // инерция до конца
  const s1 = await start(page)
  await page.evaluate(() => window.__grow(1))
  await sleep(500)
  const s2 = await start(page)
  check('1 свайп, затем рост документа: позиция держится', s1 > s0 + 200 && Math.abs(s2 - s1) <= 5,
    { s0, afterSwipe: s1, afterGrow: s2, errs })
  await page.close()
}

// --- 2. Второй свайп подряд, потом рост -------------------------------------
{
  const { page, cdp, errs } = await openStand()
  await swipe(cdp, LONG)
  await sleep(2500)
  await page.evaluate(() => window.__grow(1))
  await sleep(500)
  const a = await start(page)
  await swipe(cdp, LONG)
  await sleep(2500)
  const b = await start(page)
  await page.evaluate(() => window.__grow(1))
  await sleep(500)
  const c = await start(page)
  check('2 два свайпа с ростом между ними: второй свайп не теряется', b > a + 200 && Math.abs(c - b) <= 5,
    { afterFirst: a, afterSecond: b, afterGrow: c, errs })
  await page.close()
}

// --- 3. Документ подрос во время инерции ------------------------------------
// Итог не может оказаться выше точки, где палец отпустили.
{
  const { page, cdp, errs } = await openStand()
  await swipe(cdp, LONG)
  await sleep(2500)
  await page.evaluate(() => window.__grow(1))
  await sleep(500)
  const before = await start(page)
  await swipe(cdp, { fromY: 700, toY: 400, steps: 6, stepMs: 16 })
  const atRelease = await start(page)
  await sleep(60)
  await page.evaluate(() => window.__grow(1))
  await sleep(2500)
  const final = await start(page)
  check('3 рост документа во время инерции: назад не отбрасывает', atRelease > before + 100 && final >= atRelease - 5,
    { before, atRelease, final, errs })
  await page.close()
}

// --- 4. Регрессия: документ вырос ВЫШЕ экрана, читатель стоит ----------------
// Ради этого якорь и существует: текст перед глазами остаётся на месте, то
// есть прокрутка увеличивается ровно на выросшую высоту.
{
  const { page, cdp, errs } = await openStand()
  await swipe(cdp, LONG)
  await sleep(2500)
  const a = await start(page)
  await page.evaluate(() => window.__growAbove(300))
  await sleep(500)
  const b = await start(page)
  check('4 рост выше экрана в покое: текст остаётся перед глазами', a > 200 && Math.abs((b - a) - 300) <= 5,
    { before: a, after: b, shift: b - a, errs })
  await page.close()
}

// --- 5. Регрессия: высота контейнера меняется, ширина та же -----------------
// Вход и выход из полноэкранного режима, адресная строка (abc13c7).
{
  const { page, cdp, errs } = await openStand()
  await swipe(cdp, LONG)
  await sleep(2500)
  const a = await start(page)
  await page.evaluate(() => { window.__p.style.cssText = 'position:fixed;left:0;right:0;top:0;height:780px' })
  await sleep(500)
  const b = await start(page)
  await page.evaluate(() => { window.__p.style.cssText = 'position:fixed;inset:0' })
  await sleep(500)
  const c = await start(page)
  check('5 высота контейнера туда-обратно: позиция держится', a > 200 && Math.abs(b - a) <= 5 && Math.abs(c - a) <= 5,
    { before: a, shorter: b, restored: c, errs })
  await page.close()
}

// --- 6. Регрессия: после свайпа приходит relocate ----------------------------
// По relocate сохраняется прогресс. Свайп сразу после возврата к якорю не
// должен остаться без него.
{
  const { page, cdp, errs } = await openStand()
  await swipe(cdp, LONG)
  await sleep(2500)
  await page.evaluate(() => window.__grow(1))
  await sleep(500)
  await page.evaluate(() => { window.__relocs = [] })
  await swipe(cdp, LONG)
  await sleep(2500)
  const relocs = await page.evaluate(() => window.__relocs)
  check('6 свайп после возврата к якорю: relocate для сохранения прогресса пришёл', relocs.includes('scroll'),
    { relocs, errs })
  await page.close()
}

// --- 7. Регрессия: упор пальцем в край главы не считается прокруткой -------
// Переход в конец главы (якорь навигации = 1), палец толкает дальше — лента
// стоит. Потом документ вырос выше экрана: вид обязан остаться в конце главы,
// то есть прокрутка растёт ровно на выросшую высоту.
{
  const { page, cdp, errs } = await openStand()
  await page.evaluate(() => window.__p.goTo({ index: 0, anchor: () => 1 }))
  await sleep(800)
  const a = await start(page)
  await swipe(cdp, { fromY: 700, toY: 500, steps: 10, stepMs: 16 })
  await sleep(1500)
  const pushed = await start(page)
  await page.evaluate(() => window.__growAbove(300))
  await sleep(500)
  const b = await start(page)
  check('7 упор в конец главы, затем рост выше экрана: вид остаётся в конце', a > 1000 && Math.abs(pushed - a) <= 2 && Math.abs((b - a) - 300) <= 5,
    { atEnd: a, afterPush: pushed, afterGrow: b, shift: b - a, errs })
  await page.close()
}

await browser.close()
server.kill()
for (const r of results) console.log(JSON.stringify(r))
const failed = results.filter((r) => !r.ok)
console.log(failed.length ? `FAIL: ${failed.length} из ${results.length}` : `ok: ${results.length} из ${results.length}`)
process.exit(failed.length ? 1 : 0)
