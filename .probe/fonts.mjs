// Шрифты и поля: проверяем то, что ломает чтение молча.
//
// 1. Каждый шрифт из списка реально применяется к русскому тексту, а не
//    откатывается в системный запасной (последнее выглядит как «выбор есть, а
//    толку нет» и заметно только глазом на конкретной книге).
// 2. У каждого есть кириллица и НАСТОЯЩИЙ курсив: в прозе курсив на каждой
//    странице, а синтетический наклон её портит.
// 3. Миграция сохранённых настроек: вставка уровня полей в середину шкалы
//    сдвигает все ранее сохранённые значения, если её не мигрировать.
import puppeteer from 'puppeteer-core'
import { spawn } from 'node:child_process'
import { setTimeout as sleep } from 'node:timers/promises'

const PORT = 8253, ROOT = process.argv[2]
const server = spawn('python', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1', '--directory', ROOT], { stdio: 'ignore' })
await sleep(1200)
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
let fails = 0
const fail = (msg) => { fails++; console.log('  ПРОВАЛ ' + msg) }

// ---------- 1. Состав списка и таблицы полей ----------
const page = await browser.newPage()
await page.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
await sleep(1500)
const lists = await page.evaluate(async () => {
  const p = await import('/js/core/prefs.js')
  return {
    options: [...document.querySelectorAll('#font-family option')].map(o => o.value),
    stacks: p.FONT_STACKS,
    gfonts: p.GFONT_NAMES,
    marginName: p.MARGIN_NAME,
    marginInline: p.MARGIN_INLINE,
    marginSide: p.MARGIN_SIDE_PCT,
    marginGap: p.MARGIN_GAP,
    marginMax: p.MARGIN_MAX,
    urls: Object.keys(p.FONT_STACKS).map(k => [k, p.gfontsFor(k)]),
  }
})

console.log('\n=== список шрифтов ===')
const stackKeys = Object.keys(lists.stacks)
console.log('  в выпадающем списке:', lists.options.join(', '))
if (lists.options.join() !== stackKeys.join()) {
  fail(`список в HTML и FONT_STACKS разошлись: ${lists.options} vs ${stackKeys}`)
}
for (const [k, stack] of Object.entries(lists.stacks)) {
  if (/serif/.test(stack.replace('sans-serif', ''))) fail(`${k}: в стеке остались засечки — ${stack}`)
  if (!lists.gfonts[k] && !/system-ui/.test(stack)) fail(`${k}: нет имени для Google Fonts и нет системного запасного`)
}

console.log('\n=== поля: четыре уровня, все таблицы согласованы ===')
const levels = Object.keys(lists.marginName)
console.log('  подписи:', levels.map(l => `${l}=${lists.marginName[l]}`).join(' '))
console.log('  колонка:', JSON.stringify(lists.marginInline))
console.log('  отступ %:', JSON.stringify(lists.marginSide))
console.log('  gap:', JSON.stringify(lists.marginGap))
for (const t of ['marginInline', 'marginSide', 'marginGap']) {
  if (Object.keys(lists[t]).join() !== levels.join()) fail(`${t} не покрывает все уровни: ${Object.keys(lists[t])}`)
}
if (lists.marginMax !== levels.length - 1) fail(`MARGIN_MAX=${lists.marginMax}, а уровней ${levels.length}`)
// Уровни обязаны идти строго по возрастанию полей, иначе «+» где-то сужает поля.
const side = levels.map(l => lists.marginSide[l])
for (let i = 1; i < side.length; i++) {
  if (!(side[i] > side[i - 1])) fail(`отступы не растут: уровень ${i} = ${side[i]}%, предыдущий ${side[i - 1]}%`)
}
const inline = levels.map(l => lists.marginInline[l])
for (let i = 1; i < inline.length; i++) {
  if (!(inline[i] < inline[i - 1])) fail(`ширина колонки не сужается: уровень ${i} = ${inline[i]}`)
}
// Новый уровень 1 должен лежать МЕЖДУ соседями, а не совпадать с ними.
if (!(lists.marginSide[1] > lists.marginSide[0] && lists.marginSide[1] < lists.marginSide[2])) {
  fail(`уровень 1 (${lists.marginSide[1]}%) не между ${lists.marginSide[0]}% и ${lists.marginSide[2]}%`)
}
await page.close()

// ---------- 2. Миграция сохранённых настроек ----------
console.log('\n=== миграция сохранённых настроек ===')
for (const [tag, before, wantMargin, wantFont] of [
  ['v1: «сред.» + Merriweather', { marginLevel: 1, fontFamily: 'merriweather' }, 2, 'pt-sans'],
  ['v1: «шир.» + Lora', { marginLevel: 2, fontFamily: 'lora' }, 3, 'pt-sans'],
  ['v1: «узк.» + PT Serif', { marginLevel: 0, fontFamily: 'pt-serif' }, 0, 'pt-sans'],
  ['v1: уже sans — не трогаем', { marginLevel: 1, fontFamily: 'nunito' }, 2, 'nunito'],
  // v2 -> v3: Inter, доставшийся прошлой миграцией, переводится на новый дефолт.
  ['v2: доставшийся Inter', { v: 2, marginLevel: 2, fontFamily: 'inter' }, 2, 'pt-sans'],
  // А осознанный выбор из списка не трогаем даже при смене дефолта.
  ['v2: выбранный Rubik', { v: 2, marginLevel: 1, fontFamily: 'rubik' }, 1, 'rubik'],
  ['v3: ничего не меняем', { v: 3, marginLevel: 3, fontFamily: 'mulish' }, 3, 'mulish'],
]) {
  const p2 = await browser.newPage()
  await p2.evaluateOnNewDocument((b) => {
    localStorage.setItem('reader.prefs', JSON.stringify(b))
  }, before)
  await p2.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
  await sleep(1200)
  const got = await p2.evaluate(async () => {
    const p = await import('/js/core/prefs.js')
    return { ...p.prefs, stored: JSON.parse(localStorage.getItem('reader.prefs') || '{}') }
  })
  const ok = got.marginLevel === wantMargin && got.fontFamily === wantFont
  console.log(`  ${ok ? 'ok  ' : 'ПРОВАЛ'} ${tag}: поля ${got.marginLevel} (ждали ${wantMargin}), шрифт ${got.fontFamily} (ждали ${wantFont})`)
  if (!ok) fails++
  if (got.stored.v !== 3) fail(`  миграция не сохранена в localStorage (v=${got.stored.v})`)
  await p2.close()
}

// ---------- 3. Каждый шрифт реально применяется к русскому тексту ----------
console.log('\n=== шрифты грузятся и применяются к кириллице ===')
const p3 = await browser.newPage()
await p3.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
for (const [key, url] of lists.urls) {
  const res = await p3.evaluate(async ([key, url, stack]) => {
    if (!url) return { key, skipped: 'системный' }
    const link = document.createElement('link')
    link.rel = 'stylesheet'; link.href = url
    const loaded = new Promise((res) => { link.onload = () => res(true); link.onerror = () => res(false) })
    document.head.append(link)
    const cssOk = await loaded
    const family = stack.split(',')[0].replace(/"/g, '')
    const RU = 'Тест кириллицы, дом'
    // Файлы Google Fonts грузятся ЛЕНИВО: check() до load() отвечает false
    // даже для заведомо рабочего семейства, поэтому сначала просим загрузку
    // ровно под нужный текст и начертание.
    const loadFace = async (spec) => {
      try {
        const faces = await document.fonts.load(spec, RU)
        return faces.length > 0 && document.fonts.check(spec, RU)
      } catch { return false }
    }
    const hasRegular = await loadFace(`400 16px "${family}"`)
    await loadFace(`italic 400 16px "${family}"`)
    // check('italic ...') тут НЕ годится: Chrome синтезирует наклон для
    // семейства без italic-начертания и отвечает true независимо от того, есть
    // ли настоящий курсив. Платформенное имя шрифта синтетику тоже не выдаёт.
    // Отличает только перечисление объявленных FontFace: у семейства без
    // курсива фейса со style === 'italic' не существует вовсе.
    const hasItalic = [...document.fonts].some(
      (f) => f.family.replace(/"/g, '') === family && f.style === 'italic',
    )
    // Контроль подмены: шрифт должен РЕАЛЬНО менять отрисовку русского текста,
    // а не молча падать в системный запасной с той же метрикой.
    const measure = (css) => {
      const el = document.createElement('span')
      el.textContent = RU
      el.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:400 40px ${css}`
      document.body.append(el)
      const w = el.getBoundingClientRect().width
      el.remove()
      return w
    }
    const applied = Math.abs(measure(`"${family}", monospace`) - measure('monospace')) > 1
    return { key, cssOk, family, hasRegular, hasItalic, applied }
  }, [key, url, lists.stacks[key]])
  if (res.skipped) { console.log(`  ok   ${res.key}: ${res.skipped}`); continue }
  const bad = !res.cssOk || !res.hasRegular || !res.hasItalic || !res.applied
  if (bad) fails++
  console.log(`  ${bad ? 'ПРОВАЛ' : 'ok  '} ${res.key.padEnd(14)} css:${res.cssOk} кириллица:${res.hasRegular} курсив:${res.hasItalic} применился:${res.applied}`)
}
await p3.close()

// ---------- 4. Контроль: кириллица есть, курсива нет ----------
// Golos Text: кириллица есть, italic-фейсов ноль. Если стенд его пропускает —
// значит проверка курсива поймала синтетический наклон Chrome вместо
// настоящего начертания, и защищать состав списка ей нечем.
console.log('\n=== контроль: семейство с кириллицей, но без курсива ===')
const pItal = await browser.newPage()
await pItal.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
const noItalic = await pItal.evaluate(async () => {
  const RU = 'Тест кириллицы, дом'
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = 'https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;700&display=swap'
  const loaded = new Promise((res) => { link.onload = () => res(true); link.onerror = () => res(false) })
  document.head.append(link)
  await loaded
  await document.fonts.load('400 16px "Golos Text"', RU)
  await document.fonts.load('italic 400 16px "Golos Text"', RU).catch(() => [])
  return {
    naive: document.fonts.check('italic 400 16px "Golos Text"', RU),
    strict: [...document.fonts].some(
      (f) => f.family.replace(/"/g, '') === 'Golos Text' && f.style === 'italic',
    ),
  }
})
await pItal.close()
if (noItalic.strict) {
  fail('Golos Text прошёл проверку курсива — стенд не отличает настоящий italic')
} else {
  console.log(`  ok   Golos Text отклонён (наивная проверка сказала бы «${noItalic.naive}»)`)
}

// ---------- 5. Контроль на слепоту ----------
// Стенд, зелёный на чём угодно, ничего не доказывает: убеждаемся, что
// заведомо несуществующее семейство проверку НЕ проходит.
console.log('\n=== контроль: заведомо несуществующее семейство ===')
const p4 = await browser.newPage()
await p4.goto(`http://127.0.0.1:${PORT}/index.html`, { waitUntil: 'domcontentloaded', timeout: 40000 })
const control = await p4.evaluate(async () => {
  const RU = 'Тест кириллицы, дом'
  const measure = (css) => {
    const el = document.createElement('span')
    el.textContent = RU
    el.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:400 40px ${css}`
    document.body.append(el)
    const w = el.getBoundingClientRect().width
    el.remove()
    return w
  }
  const faces = await document.fonts.load('400 16px "NoSuchFontXYZ"', RU).catch(() => [])
  return {
    loaded: faces.length > 0,
    applied: Math.abs(measure('"NoSuchFontXYZ", monospace') - measure('monospace')) > 1,
  }
})
await p4.close()
if (control.loaded || control.applied) {
  fail(`несуществующий шрифт прошёл проверку (loaded=${control.loaded} applied=${control.applied}) — стенд слепой`)
} else {
  console.log('  ok   несуществующее семейство проверку НЕ проходит')
}

console.log(fails ? `\nПРОВАЛОВ: ${fails}` : '\nвсё в порядке')
await browser.close(); server.kill('SIGKILL')
process.exit(fails ? 1 : 0)
