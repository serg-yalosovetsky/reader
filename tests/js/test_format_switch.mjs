import assert from 'node:assert/strict'
import { formatSwitchTarget } from '../../frontend/js/core/convert.js'
for (const file_format of ['pdf', 'PDF', 'djvu', 'mobi', 'azw3', 'doc', 'docx', 'rtf']) {
  const work = { file_format }
  assert.equal(formatSwitchTarget(work, ''), null)
  assert.equal(formatSwitchTarget(work, 'epub'), file_format.toLowerCase())
  assert.equal(formatSwitchTarget(work, file_format.toLowerCase()), null)
  assert.equal(formatSwitchTarget(work, file_format.toLowerCase(), true), 'epub')
}
for (const work of [null, {}, { file_format: 'epub' }, { file_format: 'fb2' }]) {
  assert.equal(formatSwitchTarget(work, 'epub', true), null)
}
console.log('ok: format switch')
