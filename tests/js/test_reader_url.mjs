import assert from 'node:assert/strict'
import { parseReaderUrl, readerUrl } from '../../frontend/js/core/reader-url.js'
const url = path => new URL(path, 'https://reader.test')
for (const fmt of ['pdf', 'epub']) {
  const deep = parseReaderUrl(url(`/?open=42&fmt=${fmt}`))
  assert.equal(deep.id, '42')
  assert.equal(deep.fmt, fmt)
  const built = readerUrl(url('/?open=old&vpdebug=1&fmt=pdf'), '42', fmt)
  assert.equal(built, `/?vpdebug=1&fmt=${fmt}#read/42`)
  assert.equal(parseReaderUrl(url(built)).fmt, fmt)
  assert.equal(parseReaderUrl(url(built)).id, '42')
}
assert.equal(parseReaderUrl(url('/?open=42&fmt=exe')).fmt, null)
assert.equal(parseReaderUrl(url('/?fmt=pdf')), null)
assert.equal(parseReaderUrl(url('/?open=old&fmt=epub#read/new')).id, 'new')
assert.equal(parseReaderUrl(url('/#book/42')).page, true)
assert.equal(readerUrl(url('/?fmt=pdf&original=1'), 'other'), '/#read/other')
assert.equal(parseReaderUrl(url(readerUrl(url('/'), 'книга / 1'))).id, 'книга / 1')
console.log('ok: reader URL')
