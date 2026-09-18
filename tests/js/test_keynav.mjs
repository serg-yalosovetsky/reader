// Клавиши навигации в читалке (serg/tasks#996).
//
// Запуск: node tests/js/test_keynav.mjs
// Поручение владельца: End — в конец ГЛАВЫ, Ctrl+End — в конец КНИГИ; то же с
// Home. Раньше Home/End уводили сразу к началу и концу КНИГИ (goToFraction 0/1),
// и добраться до конца длинной главы одной клавишей было нечем.
//
// Проверяется НАСТОЯЩИЙ frontend/js/core/keynav.js — чистый модуль без импортов
// (тем же приёмом, что core/fullscreen.js: сам navigation.js тянет десяток
// зависимостей и в node не грузится).
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const kn = await import(
  pathToFileURL(new URL('../../frontend/js/core/keynav.js', import.meta.url).pathname).href
)

// --- Поручение #996 дословно ------------------------------------------------
assert.equal(kn.keyNavAction('End', {}), 'chapterEnd')
assert.equal(kn.keyNavAction('End', { ctrl: true }), 'bookEnd')
assert.equal(kn.keyNavAction('Home', {}), 'chapterStart')
assert.equal(kn.keyNavAction('Home', { ctrl: true }), 'bookStart')

// --- Прежнее листание не сломано --------------------------------------------
assert.equal(kn.keyNavAction('ArrowLeft', {}), 'prev')
assert.equal(kn.keyNavAction('ArrowRight', {}), 'next')
assert.equal(kn.keyNavAction('PageUp', {}), 'prev')
assert.equal(kn.keyNavAction('PageDown', {}), 'next')
assert.equal(kn.keyNavAction(' ', {}), 'next')
assert.equal(kn.keyNavAction(' ', { shift: true }), 'prev')
assert.equal(kn.keyNavAction('Spacebar', {}), 'next')
assert.equal(kn.keyNavAction('Enter', {}), 'next')
assert.equal(kn.keyNavAction('Enter', { shift: true }), 'prev')

// --- Ctrl модифицирует ТОЛЬКО Home/End --------------------------------------
// Ctrl+PageDown у браузера — переключение вкладок, Ctrl+стрелка — переход по
// словам. Перехватывать их читалка не должна: владелец просил Ctrl только для
// Home/End, а молчаливый угон чужого сочетания заметят не сразу.
assert.equal(kn.keyNavAction('PageDown', { ctrl: true }), null)
assert.equal(kn.keyNavAction('PageUp', { ctrl: true }), null)
assert.equal(kn.keyNavAction('ArrowRight', { ctrl: true }), null)
assert.equal(kn.keyNavAction('ArrowLeft', { ctrl: true }), null)
assert.equal(kn.keyNavAction(' ', { ctrl: true }), null)
assert.equal(kn.keyNavAction('Enter', { ctrl: true }), null)

// --- Чужие клавиши не трогаем ------------------------------------------------
assert.equal(kn.keyNavAction('a', {}), null)
assert.equal(kn.keyNavAction('Escape', {}), null)
assert.equal(kn.keyNavAction('Tab', {}), null)
assert.equal(kn.keyNavAction(undefined, {}), null)

// --- Вызов без второго аргумента не падает -----------------------------------
assert.equal(kn.keyNavAction('End'), 'chapterEnd')
assert.equal(kn.keyNavAction('Home'), 'chapterStart')

console.log('ok: test_keynav.mjs')
