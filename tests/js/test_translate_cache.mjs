// Кэш переводов во вкладке: повторное включение не должно ходить в сеть.
//
// Запуск: node tests/js/test_translate_cache.mjs
// Проверяется НАСТОЯЩИЙ frontend/js/translate.js; DOM, сеть и шапка — заглушки
// (tests/js/hooks.mjs подменяет только импорты). Браузер для этого не нужен, а
// без такой проверки «мгновенное повторное включение» подтверждается только
// рассуждением.
import { register } from 'node:module'
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

register('./hooks.mjs', import.meta.url)

const { calls, setReply } = await import('./stub/api.mjs')
const state = await import('./stub/state.mjs')
const tr = await import(
  pathToFileURL(new URL('../../frontend/js/translate.js', import.meta.url).pathname).href
)

// --- крошечный DOM: ровно то, чем пользуется translate.js ---
function makeEl(text) {
  const e = {
    textContent: text,
    dataset: {},
    getBoundingClientRect: () => ({ width: 100, height: 20, top: 0, bottom: 20, left: 0, right: 100 }),
    append(frag) { e.textContent = frag.text },
  }
  Object.defineProperty(e, 'childNodes', {
    get: () => [{ cloneNode: () => ({ text: e.textContent }) }],
  })
  e.ownerDocument = {
    createDocumentFragment() {
      const f = {
        parts: [], text: '',
        append(n) { f.parts.push(n); f.text = f.parts.map((p) => p.text).join('') },
        cloneNode() { return { text: f.text } },
      }
      return f
    },
  }
  return e
}

function makeDoc(texts) {
  const els = texts.map(makeEl)
  return {
    els,
    defaultView: { innerWidth: 800, innerHeight: 600 },
    querySelectorAll(sel) {
      return sel === '[data-tr-on]' ? els.filter((e) => e.dataset.trOn) : els
    },
  }
}

const tick = () => new Promise((r) => setTimeout(r, 0))

// Сервер: русские абзацы помечает skipped, «сломанный» — failed, прочие переводит.
function serverReply(broken = null) {
  return (body) => {
    let translated = 0, failed = 0, skipped = 0
    const items = body.items.map((it) => {
      if (it.text.startsWith('RU ')) { skipped++; return { id: it.id, text: it.text, changed: false, skipped: true } }
      if (it.text === broken) { failed++; return { id: it.id, text: it.text, changed: false, skipped: false } }
      translated++
      return { id: it.id, text: 'ПЕРЕВОД ' + it.text, changed: true }
    })
    return { items, translated, cached: 0, skipped, failed }
  }
}

async function scenario(name, fn) {
  calls.length = 0
  await fn()
  console.log('  ok —', name)
}

console.log('кэш переводов во вкладке:')

await scenario('первое включение идёт в сеть и подменяет текст', async () => {
  const doc = makeDoc(['Hello world', 'RU Привет мир', 'Second paragraph'])
  state.setBookDoc(doc)
  setReply(serverReply())
  tr.toggleTranslate()
  await tick(); await tick()
  assert.equal(calls.length, 1, 'ожидался ровно один запрос')
  assert.equal(doc.els[0].textContent, 'ПЕРЕВОД Hello world')
  assert.equal(doc.els[1].textContent, 'RU Привет мир', 'русский абзац не трогаем')

  // Возврат оригинала — без сети.
  tr.toggleTranslate()
  await tick()
  assert.equal(calls.length, 1, 'возврат оригинала не должен ходить в сеть')
  assert.equal(doc.els[0].textContent, 'Hello world')

  // И снова перевод: всё уже известно вкладке.
  tr.toggleTranslate()
  await tick(); await tick()
  assert.equal(calls.length, 1, 'повторное включение обязано обойтись без сети')
  assert.equal(doc.els[0].textContent, 'ПЕРЕВОД Hello world')
  assert.equal(doc.els[2].textContent, 'ПЕРЕВОД Second paragraph')
  tr.resetTranslate()
})

await scenario('кэш переживает перезагрузку главы (ключ по тексту)', async () => {
  const again = makeDoc(['Hello world', 'Second paragraph'])
  state.setBookDoc(again)
  setReply(serverReply())
  tr.toggleTranslate()
  await tick(); await tick()
  assert.equal(calls.length, 0, 'те же абзацы в новом документе — из кэша')
  assert.equal(again.els[0].textContent, 'ПЕРЕВОД Hello world')
  tr.resetTranslate()
})

await scenario('упавший абзац не попадает в «переводить не надо»', async () => {
  // `done` — пометка на УЗЛЕ, она живёт до перезагрузки главы и одинаково
  // гасит и упавшие, и уже-русские абзацы. Разница между ними видна на новом
  // документе: русский помнится по ТЕКСТУ (skipCache) и больше не спрашивается
  // никогда, а упавший обязан уйти в запрос снова.
  const doc = makeDoc(['RU Уже по-русски', 'Broken paragraph'])
  state.setBookDoc(doc)
  setReply(serverReply('Broken paragraph'))
  tr.toggleTranslate()
  await tick(); await tick()
  assert.equal(calls.length, 1)
  assert.equal(doc.els[1].textContent, 'Broken paragraph', 'упавший абзац остаётся оригиналом')
  tr.resetTranslate()

  const fresh = makeDoc(['RU Уже по-русски', 'Broken paragraph'])
  state.setBookDoc(fresh)
  setReply(serverReply())  // движок починился
  tr.toggleTranslate()
  await tick(); await tick()
  assert.equal(calls.length, 2, 'после перезагрузки главы упавший абзац спрашиваем снова')
  assert.deepEqual(calls[1].items, ['Broken paragraph'],
    'русский абзац не должен попадать в повторный запрос (it.skipped)')
  assert.equal(fresh.els[1].textContent, 'ПЕРЕВОД Broken paragraph')
  tr.resetTranslate()
})

console.log('всё зелёное')
