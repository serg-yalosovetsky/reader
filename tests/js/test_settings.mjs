import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// Проверяем переключение настроек между исходным PDF, EPUB-версией и обычной книгой.
const nodes = new Map()
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {
    row: {}, style: {}, listeners: {},
    closest() { return this.row },
    addEventListener(type, callback) { this.listeners[type] = callback },
  })
  return nodes.get(id)
}
const listeners = {}
const swatch = { ...node('swatch'), dataset: { theme: 'night' }, setAttribute() {} }
const prefs = { menuAutohidePdf: true, menuAutohideOther: false, fontScale: 1, marginLevel: 2 }
let applied = 0, refreshed = 0, saved = 0
const context = globalThis.settingsTest = {
  prefs, currentFormat: 'pdf', currentWork: { file_format: 'pdf' },
  $: node, savePrefs: () => saved++, MARGIN_NAME: { 2: 'сред.' }, MARGIN_MAX: 3,
  applyViewStyles: () => applied++, refreshMenuAutohide: () => refreshed++,
  pdfAsEpub: () => true, setPdfAsEpub() {},
}
globalThis.document = {
  documentElement: { dataset: {} },
  querySelectorAll: () => [swatch],
  addEventListener: (type, callback) => { listeners[type] = callback },
}
let source = readFileSync(new URL('../../frontend/js/settings.js', import.meta.url), 'utf8')
source = source.replace(/^import .*$/gm, '')
  .replace(/\bcurrentFormat\b/g, 'context.currentFormat')
  .replace(/\bcurrentWork\b/g, 'context.currentWork')
source = `const context = globalThis.settingsTest;
const {prefs, $, savePrefs, MARGIN_NAME, MARGIN_MAX, applyViewStyles, refreshMenuAutohide, pdfAsEpub, setPdfAsEpub} = context;\n` + source
const { syncSettingsUI } = await import('data:text/javascript,' + encodeURIComponent(source))
syncSettingsUI()
assert.equal(node('#menu-autohide').checked, true)
assert.equal(node('#pdf-crop').row.hidden, false)
assert.equal(node('#pdf-epub').row.hidden, true)
node('#menu-autohide').listeners.change({ target: { checked: false } })
assert.equal(prefs.menuAutohidePdf, false)
assert.equal(refreshed, 1)
context.currentFormat = 'epub'
listeners['reader-format-change']()
assert.equal(node('#menu-autohide').checked, false)
assert.equal(node('#pdf-crop').row.hidden, true)
assert.equal(node('#pdf-epub').row.hidden, false)
node('#menu-autohide').listeners.change({ target: { checked: true } })
assert.equal(prefs.menuAutohideOther, true)
assert.equal(prefs.menuAutohidePdf, false)
context.currentWork = { file_format: 'epub' }
listeners['reader-format-change']()
assert.equal(node('#pdf-epub').row.hidden, true)
context.currentFormat = 'pdf'
listeners['reader-format-change']()
assert.equal(node('#menu-autohide').checked, false)
swatch.listeners.click()
assert.equal(prefs.theme, 'night')
assert.equal(document.documentElement.dataset.theme, 'night')
assert.equal(applied, 1)
assert.equal(saved, 3)
console.log('ok: настройки текущего формата и смена темы')
