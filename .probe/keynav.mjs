// Клавиши End/Home в живом браузере (serg/tasks#996).
//
// Запуск (на VPS, мимо SSO): CHROME_PATH=… node .probe/keynav.mjs http://127.0.0.1:8123
// Код выхода ненулевой, если хоть одна проверка упала.
//
// Проверяется НАСТОЯЩАЯ книга владельца (work 961 «Ring-Maker», 181 глава):
// tests/js/test_keynav.mjs доказывает только РЕШЕНИЕ по клавише, а сам переход
// выполняет foliate — это видно лишь в браузере.
//
// Две ловушки, на которых первая версия стенда врала:
//   1. индекс текущей главы в событии relocate зовётся `section.current`;
//      поля `index` там нет, и сравнение undefined === undefined давало
//      ЛОЖНЫЙ «ok» (этим же был сломан lastIdx во фронте);
//   2. `fraction` в detail — доля от ВСЕЙ книги, а не от главы. «Конец главы»
//      проверяется тем, что глава та же, позиция выросла, но до конца книги не
//      доехала.
//
// Стенд трогает живые данные: открытие книги и прыжки перезаписывают сохранённую
// позицию чтения, поэтому прогресс снимается ДО и возвращается в finally, даже
// при падении. Побочный эффект, который вернуть нельзя: PUT /api/progress
// обновляет work.updated_at, то есть книга поднимется в сортировке библиотеки.
import puppeteer from 'puppeteer-core'
import { setTimeout as sleep } from 'node:timers/promises'

const BASE = process.argv[2] || 'http://127.0.0.1:8123'
const WORK_ID = Number(process.argv[3] || 961)

const results = []
const check = (name, ok, detail) => results.push({ name, ok, ...detail })

const saved = await fetch(`${BASE}/api/progress/${WORK_ID}`).then(r => r.json())
const restoreBody = {
  ratio: saved?.ratio ?? 0,
  locator: saved?.locator ?? '',
  text_anchor: saved?.text_anchor ?? '',
}

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
// Десктопный экран: проверяем именно КЛАВИАТУРУ.
await page.setViewport({ width: 1280, height: 900 })
const errs = []
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
// Текст console-ошибки URL не содержит — чужой шум отделяем по ответам сети.
const http4xx = []
page.on('response', (r) => { if (r.status() >= 400) http4xx.push(`${r.status()} ${r.url()}`) })

const state = () => page.evaluate(() => window.__keynavProbe?.last ?? null)
const press = async (key, ctrl = false) => {
  if (ctrl) await page.keyboard.down('Control')
  await page.keyboard.press(key)
  if (ctrl) await page.keyboard.up('Control')
  await sleep(2000)
}

try {
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 40000 })
  await sleep(4500)

  const opened = await page.evaluate((id) => {
    const cards = [...document.querySelectorAll('#book-grid .book-card')]
    const mine = cards.find((c) => c._w && c._w.id === id)
    if (!mine) return { ok: false, cards: cards.length }
    mine.click()
    return { ok: true, cards: cards.length }
  }, WORK_ID)
  check('карточка книги найдена', opened.ok, opened)
  await sleep(1800)

  await page.evaluate(() => document.getElementById('bp-read')?.click())
  await page.waitForFunction(() => {
    const v = document.querySelector('foliate-view')
    return v && v.renderer && !document.getElementById('reader').hidden
  }, { timeout: 90000 })

  // Индекс главы — section.current (см. SectionProgress.getProgress).
  await page.evaluate(() => {
    const v = document.querySelector('foliate-view')
    window.__keynavProbe = { last: null }
    v.addEventListener('relocate', (e) => {
      window.__keynavProbe.last = {
        chapter: e.detail?.section?.current ?? null,
        fraction: e.detail?.fraction ?? null,
      }
    })
  })
  await sleep(2500)

  const edges = await page.evaluate(() => {
    const secs = document.querySelector('foliate-view')?.renderer?.sections || []
    return {
      first: secs.findIndex((s) => s?.linear !== 'no'),
      last: secs.findLastIndex((s) => s?.linear !== 'no'),
      total: secs.length,
    }
  })
  check('главы книги видны стенду', edges.total > 5 && edges.last > edges.first, edges)

  // Старт — середина книги: только так «конец главы» отличим от «конца книги».
  await page.evaluate((i) => document.querySelector('foliate-view')
    .renderer.goTo({ index: i, anchor: () => 0 }), Math.floor(edges.last / 2))
  await sleep(2500)
  const start = await state()
  check('индекс главы вообще приходит', typeof start?.chapter === 'number', { start })
  check('стартовали в середине книги',
    start?.chapter > edges.first && start?.chapter < edges.last, { chapter: start?.chapter })

  // --- End: конец ТЕКУЩЕЙ главы ---------------------------------------------
  await press('End')
  const afterEnd = await state()
  check('End оставил ту же главу', afterEnd?.chapter === start?.chapter,
    { was: start?.chapter, now: afterEnd?.chapter })
  check('End продвинул вперёд внутри главы', afterEnd?.fraction > start?.fraction,
    { was: start?.fraction, now: afterEnd?.fraction })
  check('End НЕ уехал в конец книги', afterEnd?.fraction < 0.99,
    { fraction: afterEnd?.fraction })

  // --- Home: начало ТЕКУЩЕЙ главы -------------------------------------------
  await press('Home')
  const afterHome = await state()
  check('Home оставил ту же главу', afterHome?.chapter === start?.chapter,
    { now: afterHome?.chapter })
  check('Home вернул назад внутри главы', afterHome?.fraction < afterEnd?.fraction,
    { now: afterHome?.fraction })
  check('Home НЕ уехал в начало книги', afterHome?.fraction > 0.01,
    { fraction: afterHome?.fraction })

  // --- Ctrl+End: конец КНИГИ -------------------------------------------------
  await press('End', true)
  const afterCtrlEnd = await state()
  check('Ctrl+End увёл в последнюю главу книги', afterCtrlEnd?.chapter === edges.last,
    { expected: edges.last, now: afterCtrlEnd?.chapter })

  // --- Ctrl+Home: начало КНИГИ ----------------------------------------------
  await press('Home', true)
  const afterCtrlHome = await state()
  check('Ctrl+Home увёл в первую главу книги', afterCtrlHome?.chapter === edges.first,
    { expected: edges.first, now: afterCtrlHome?.chapter })
  check('Ctrl+Home встал в начало книги', afterCtrlHome?.fraction < 0.01,
    { fraction: afterCtrlHome?.fraction })

  // Обложки чужих книг библиотеки отдают 404 (serg/tasks#997) — это фон, не про
  // клавиши. Свой шум не прячем, но и чужой за свой не выдаём.
  const foreign = http4xx.filter((u) => /\/cover/.test(u))
  const ownHttp = http4xx.filter((u) => !/\/cover/.test(u))
  check('лишних сетевых ошибок нет', ownHttp.length === 0,
    { own: ownHttp.slice(0, 3), чужих404обложек: foreign.length })
  const jsErrs = errs.filter((s) => s.startsWith('pageerror'))
  check('ошибок JS на странице нет', jsErrs.length === 0, { errs: jsErrs.slice(0, 3) })
} finally {
  await browser.close()
  const r = await fetch(`${BASE}/api/progress/${WORK_ID}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(restoreBody),
  })
  const back = await fetch(`${BASE}/api/progress/${WORK_ID}`).then(x => x.json()).catch(() => null)
  console.log('прогресс возвращён:', r.status,
    'ratio', back?.ratio, '| было', restoreBody.ratio,
    '| locator совпал:', back?.locator === restoreBody.locator)
}

let bad = 0
for (const r of results) {
  const { name, ok, ...rest } = r
  if (!ok) bad++
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`, Object.keys(rest).length ? JSON.stringify(rest) : '')
}
console.log(`итого: ok ${results.length - bad} из ${results.length}`)
process.exit(bad ? 1 : 0)
