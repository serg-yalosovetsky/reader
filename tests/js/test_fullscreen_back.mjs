// Полноэкранный режим читалки и системное «назад» (serg/tasks#902, #903).
//
// Запуск: node tests/js/test_fullscreen_back.mjs
// На Android первое «назад» в полноэкранном режиме браузер тратит на выход из
// него — popstate не приходит, и до библиотеки нужно было нажимать дважды.
// Проверяется НАСТОЯЩИЙ frontend/js/core/fullscreen.js (у модуля нет импортов).
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const fs = await import(
  pathToFileURL(new URL('../../frontend/js/core/fullscreen.js', import.meta.url).pathname).href
)

// Выход не нашей кнопкой, книга открыта, сенсорный экран → это «назад».
assert.equal(fs.exitMeansBack({ readerOpen: true, byButton: false, coarse: true }), true)
// Выход кнопкой ✖ — просто выход из полноэкранного режима, книга остаётся.
assert.equal(fs.exitMeansBack({ readerOpen: true, byButton: true, coarse: true }), false)
// В библиотеке закрывать нечего.
assert.equal(fs.exitMeansBack({ readerOpen: false, byButton: false, coarse: true }), false)
// Esc на десктопе — просто выход: закрывать книгу по Esc никто не просил.
assert.equal(fs.exitMeansBack({ readerOpen: true, byButton: false, coarse: false }), false)

// Флаг «выход кнопкой/программой» одноразовый.
fs.markButtonExit()
assert.equal(fs.takeButtonExit(), true)
assert.equal(fs.takeButtonExit(), false)

// Программный выход (closeReader) помечает себя, чтобы не считаться «назад».
const fakeDoc = {
  fullscreenElement: {},
  exitFullscreen() { this.fullscreenElement = null; return Promise.resolve() },
}
assert.equal(fs.isFullscreen(fakeDoc), true)
fs.exitFullscreen(fakeDoc)
assert.equal(fs.isFullscreen(fakeDoc), false)
assert.equal(fs.takeButtonExit(), true)
// Не в полноэкранном режиме — ничего не делает и флаг не ставит.
fs.exitFullscreen(fakeDoc)
assert.equal(fs.takeButtonExit(), false)

console.log('ok: fullscreen back')
