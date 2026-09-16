// Полноэкранный режим читалки и системное «назад» (serg/tasks#902, #903, #927).
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

// --- Гашение экрана телефона и уход в фон (serg/tasks#927) -------------------
// Блокировка экрана снимает полноэкранный режим ровно так же, как системное
// «назад»: не нашей кнопкой, на сенсорном экране, при открытой книге. Живой
// случай: Серж гасил экран во время чтения, а при разблокировке оказывался в
// библиотеке. Отличаем по видимости страницы — жест человека приходит на
// ВИДИМОЙ странице и не рядом с переключением видимости.

// Страница скрыта прямо сейчас (экран погас) — книгу не закрываем.
assert.equal(fs.exitMeansBack({
  readerOpen: true, byButton: false, coarse: true, docHidden: true,
}), false)

// Только что вернулись из фона (экран включили) — выход из режима сделал
// браузер, а не человек.
assert.equal(fs.exitMeansBack({
  readerOpen: true, byButton: false, coarse: true,
  docHidden: false, msSinceVisibilityChange: 200,
}), false)

// Страница давно на виду — значит это действительно «назад».
assert.equal(fs.exitMeansBack({
  readerOpen: true, byButton: false, coarse: true,
  docHidden: false, msSinceVisibilityChange: 60000,
}), true)

// Ровно на границе окна ожидания — ещё не «назад» (граница включительно).
assert.equal(fs.exitMeansBack({
  readerOpen: true, byButton: false, coarse: true,
  docHidden: false, msSinceVisibilityChange: fs.VISIBILITY_GRACE_MS - 1,
}), false)
assert.equal(fs.exitMeansBack({
  readerOpen: true, byButton: false, coarse: true,
  docHidden: false, msSinceVisibilityChange: fs.VISIBILITY_GRACE_MS,
}), true)

// Страница ни разу не меняла видимость (msSinceVisibilityChange не задан) —
// это обычное «назад», а не подозрительный выход.
assert.equal(fs.exitMeansBack({
  readerOpen: true, byButton: false, coarse: true,
  docHidden: false, msSinceVisibilityChange: null,
}), true)

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
