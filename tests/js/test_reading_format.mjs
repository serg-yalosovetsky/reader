import assert from 'node:assert/strict'
import { readingFormat, formatRecord, newerReadingFormat, localReadingFormat, storeReadingFormat } from '../../frontend/js/core/convert.js'

for (const file_format of ['pdf', 'PDF', 'djvu', 'mobi', 'azw3', 'doc', 'docx', 'rtf']) {
  const work = { file_format }
  const original = file_format.toLowerCase()
  const rec = (file_format, saved_at = 10) => ({ file_format, saved_at })
  // Без истории — общая настройка; с историей — последний формат книги.
  assert.equal(readingFormat(work), 'epub')
  assert.equal(readingFormat(work, {}, null, null, false), original)
  for (const preference of [true, false]) {
    assert.equal(readingFormat(work, {}, rec(original), rec('epub', 100), preference), original)
    assert.equal(readingFormat(work, {}, null, rec('EPUB'), preference), 'epub')
    assert.equal(readingFormat(work, { original: true }, rec('epub'), rec('epub'), preference), original)
    assert.equal(readingFormat(work, { fmt: original }, rec('epub'), rec('epub'), preference), original)
    assert.equal(readingFormat(work, { fmt: 'epub' }, rec(original), rec(original), preference), 'epub')
    assert.equal(readingFormat(work, { fmt: 'epub', original: true }, rec(original), rec(original), preference), 'epub')
    assert.equal(readingFormat(work, {}, rec('fb2'), rec('txt'), preference), preference ? 'epub' : original)
    assert.equal(readingFormat(work, {}, rec('txt'), rec(original), preference), original)
  }
}
for (const file_format of ['epub', 'fb2', 'txt']) {
  assert.equal(readingFormat({ file_format }, {}, 'pdf'), file_format)
  assert.equal(readingFormat({ file_format }, { original: true }, 'epub'), file_format)
}
assert.equal(readingFormat(null), '')
// Локальная запись отдельная на книгу и сохраняет время для позднего ответа.
const storage = new Map()
globalThis.localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, value),
}
const pdf = { id: 1, file_format: 'pdf' }
assert.equal(localReadingFormat(pdf), null)
storeReadingFormat(pdf, { file_format: 'PDF', saved_at: 42 })
assert.deepEqual(localReadingFormat(pdf), { file_format: 'pdf', saved_at: 42 })
assert.equal(localReadingFormat({ ...pdf, id: 2 }), null)
storeReadingFormat(pdf, { file_format: 'epub', saved_at: 100 })
assert.deepEqual(localReadingFormat(pdf), { file_format: 'epub', saved_at: 100 })
assert.equal(formatRecord(pdf, { file_format: 'fb2', saved_at: 200 }), null)
const local = { file_format: 'pdf', saved_at: 100 }
for (const saved_at of [99, 100, 101]) {
  const server = { file_format: 'epub', saved_at }
  // В этом открытии локальный формат побеждает даже более свежий серверный.
  assert.equal(readingFormat(pdf, {}, local, server), 'pdf')
  // Поздний ответ обновляет только историю следующего открытия.
  const next = newerReadingFormat(pdf, local, server)
  assert.equal(readingFormat(pdf, {}, next), saved_at > 100 ? 'epub' : 'pdf')
}
assert.deepEqual(newerReadingFormat(pdf, local, null), local)
assert.deepEqual(newerReadingFormat(pdf, null, local), local)
console.log('ok: reading format')
