import assert from 'node:assert/strict'
import { positionForFormat, restorePosition, gotoPosition } from '../../frontend/js/core/position.js'

for (const [saved, opened] of [['pdf', 'epub'], ['epub', 'pdf']]) {
  const pos = { file_format: saved, ratio: 0.62, locator: 'epubcfi(/6/8!/4)', text_anchor: 'старый текст' }
  assert.deepEqual(positionForFormat(pos, opened), { ratio: 0.62 })
  assert.equal(positionForFormat(pos, saved), pos)
}
assert.deepEqual(positionForFormat({ locator: 'epubcfi(/6/8)', ratio: 0.5 }, 'epub'), { ratio: 0.5 })
assert.deepEqual(positionForFormat({ locator: 'epubcfi(/6/8!/4)', ratio: 0.5 }, 'pdf'), { ratio: 0.5 })
const calls = []
const view = {
  book: { sections: [{}, {}] },
  resolveNavigation: () => undefined,
  init: async opts => calls.push(opts),
  goToFraction: async ratio => calls.push(ratio),
}
assert.equal(await restorePosition(view, { ratio: 0.62, locator: 'broken' }), true)
assert.deepEqual(calls, [{ showTextStart: true }, 0.62])
calls.length = 0
assert.equal(await gotoPosition(view, { ratio: 0.62, locator: 'broken' }), true)
assert.deepEqual(calls, [0.62])
for (const [saved, opened] of [['pdf', 'epub'], ['epub', 'pdf']]) {
  calls.length = 0
  assert.equal(await restorePosition(view, {
    ratio: 0.62, file_format: saved, locator: 'epubcfi(/6/8!/4)',
  }, '', opened), true)
  assert.deepEqual(calls, [{ showTextStart: true }, 0.62])
}
// Индекс за пределами книги и битый путь внутри существующей секции.
for (const resolved of [{ index: 99 }, { index: 0, anchor: () => { throw Error('CFI') } }]) {
  calls.length = 0
  view.resolveNavigation = () => resolved
  view.book.sections[0].createDocument = async () => ({})
  assert.equal(await restorePosition(view, { ratio: 0.62, locator: 'broken' }), true)
  assert.deepEqual(calls, [{ showTextStart: true }, 0.62])
}
view.goToFraction = async () => { throw Error('failed') }
assert.equal(await restorePosition(view, { ratio: 0.62 }), false)
console.log('ok: выбор позиции при смене формата и неразрешимом locator')
// PDF-лента: при F5 CFI страницы не должен вытеснять точную долю внутри неё.
const pdfCalls = []
const pdfView = {
  isFixedLayout: true,
  renderer: { scrolled: true, getContents: () => [] },
  book: { sections: Array(20).fill({}) },
  resolveNavigation: target => target?.fraction != null ? {index: 10, anchor: .5} : {index: 10},
  init: async opts => pdfCalls.push(opts),
  goTo: async target => { pdfCalls.push(target); return {index: 10} },
}
await restorePosition(pdfView, {locator: 'epubcfi(/6/22)', ratio: .525, book_rev: 'same'}, 'same', 'pdf')
assert.deepEqual(pdfCalls, [{lastLocation: {fraction: .525}}])
