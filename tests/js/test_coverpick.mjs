// Ручная обложка: чистая логика выбора и отказов (serg/tasks#1055).
//
// Запуск: node tests/js/test_coverpick.mjs
// Проверяется НАСТОЯЩИЙ frontend/js/core/coverpick.js — модуль без импортов
// (тем же приёмом, что core/keynav.js): cover-picker.js тянет DOM и сеть.
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const cp = await import(
  pathToFileURL(new URL('../../frontend/js/core/coverpick.js', import.meta.url).pathname).href
)

const MB = 1024 * 1024
const file = (size, type) => ({ size, type })

// --- клиентская проверка файла: быстрый отказ до отправки ---------------------
assert.equal(cp.checkCoverFile(file(300 * 1024, 'image/png')).ok, true)
assert.equal(cp.checkCoverFile(file(300 * 1024, 'image/jpeg')).ok, true)
assert.equal(cp.checkCoverFile(file(300 * 1024, 'image/webp')).ok, true)
// граница: ровно 10 МБ ещё можно, на байт больше — нельзя (как на сервере)
assert.equal(cp.checkCoverFile(file(10 * MB, 'image/png')).ok, true)
assert.equal(cp.checkCoverFile(file(10 * MB + 1, 'image/png')).ok, false)
assert.match(cp.checkCoverFile(file(10 * MB + 1, 'image/png')).reason, /10 МБ/)
assert.equal(cp.checkCoverFile(file(0, 'image/png')).ok, false)
assert.equal(cp.checkCoverFile(null).ok, false)
// не картинка отбраковывается сразу…
assert.equal(cp.checkCoverFile(file(1000, 'application/pdf')).ok, false)
assert.equal(cp.checkCoverFile(file(1000, 'text/html')).ok, false)
// …а пустой тип — нет: часть Android-менеджеров файлов его не сообщает,
// решать будет сервер по содержимому
assert.equal(cp.checkCoverFile(file(1000, '')).ok, true)

// --- поиск книги: слова в любом порядке, регистр и «ё» не важны ---------------
const books = [
  { id: 1, title: 'Властелин колец', author: 'Джон Толкин' },
  { id: 2, title: 'The Lord of the Rings', author: 'John Ronald Reuel Tolkien' },
  { id: 3, title: 'Ёжик в тумане', author: '' },
  { id: 4, title: 'Дюна', author: 'Фрэнк Герберт' },
]
assert.equal(cp.matchBook(books[0], ''), true)
assert.equal(cp.matchBook(books[0], '   '), true)
assert.equal(cp.matchBook(books[1], 'tolkien lord'), true)   // порядок слов не важен
assert.equal(cp.matchBook(books[1], 'TOLKIEN'), true)        // регистр не важен
assert.equal(cp.matchBook(books[0], 'толкин властелин'), true)
assert.equal(cp.matchBook(books[2], 'ежик'), true)           // «ё» = «е»
assert.equal(cp.matchBook(books[3], 'дюна толкин'), false)   // каждое слово обязано найтись
assert.equal(cp.matchBook({ id: 9 }, 'что-то'), false)       // ни названия, ни автора — не падаем

// --- список для окна выбора ---------------------------------------------------
assert.deepEqual(cp.pickableBooks(books, 1, '').map((b) => b.id), [2, 3, 4])  // себя не предлагаем
assert.deepEqual(cp.pickableBooks(books, 1, 'tolkien').map((b) => b.id), [2])
assert.deepEqual(cp.pickableBooks(books, 99, 'герберт').map((b) => b.id), [4])
assert.deepEqual(cp.pickableBooks(books, 1, 'нет такой'), [])
const many = Array.from({ length: 200 }, (_, i) => ({ id: i + 1, title: `Книга ${i}`, author: '' }))
assert.equal(cp.pickableBooks(many, 1, '').length, cp.COVER_PICK_LIMIT)        // не рисуем 200 карточек
assert.equal(cp.pickableBooks(many, 1, '', 5).length, 5)
assert.equal(cp.pickableBooks(many, 1, '').some((b) => b.id === 1), false)

// --- что сказать человеку при отказе сервера ----------------------------------
assert.match(cp.describeCoverError(409), /нет обложки/)
assert.match(cp.describeCoverError(413), /большой/)
assert.match(cp.describeCoverError(415), /JPEG, PNG или WebP/)
assert.match(cp.describeCoverError(422), /повреждена|маленькая/)
assert.equal(cp.describeCoverError(413, 'файл больше 10 МБ'), 'файл больше 10 МБ')  // текст сервера важнее
assert.match(cp.describeCoverError(500), /500/)
assert.equal(typeof cp.describeCoverError(422, { нестрока: 1 }), 'string')          // detail бывает объектом

// --- адрес обложки: версия есть всегда ----------------------------------------
assert.equal(cp.coverUrl(7, 123), '/api/reader/7/cover?v=123')
assert.equal(cp.coverUrl(7, 123, 320), '/api/reader/7/cover?w=320&v=123')
assert.equal(cp.coverUrl(7, undefined), '/api/reader/7/cover?v=0')
assert.notEqual(cp.coverUrl(7, 1), cp.coverUrl(7, 2))  // новая версия = новый URL для кеша

console.log('ok test_coverpick')
