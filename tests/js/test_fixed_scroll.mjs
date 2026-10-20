import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
const source = readFileSync(new URL('../../frontend/vendor/foliate-js/fixed-scroll.js', import.meta.url), 'utf8')
const { fromFraction, toFraction, scrollPosition, visiblePages } = await import('data:text/javascript,' + encodeURIComponent(source))
for (const fraction of [0, .0123, .5, .999, 1]) {
  const { index, offset } = fromFraction(fraction, 300)
  assert.ok(Math.abs(toFraction(index, offset, 300) - fraction) < 1e-12)
}
assert.deepEqual(fromFraction(.5, 20), {index: 10, offset: 0})
assert.deepEqual(fromFraction(1, 20), {index: 19, offset: 1})
assert.deepEqual(scrollPosition([100, 200, 50], 200), {index: 1, offset: .5})
// scrollTop, округлённый вниз на долю пикселя, остаётся на целевой странице
assert.deepEqual(scrollPosition([549.33, 549.33], 549.14).index, 1)
assert.deepEqual(scrollPosition([549.33, 549.33], 547).index, 0)
assert.deepEqual(visiblePages([100, 200, 50, 100, 100], 110, 100), [0, 1, 2])
assert.deepEqual(visiblePages(Array(300).fill(1000), 10100, 800), [9, 10, 11])
assert.deepEqual(visiblePages([100, 100], 0, 500), [0, 1])
console.log('Fixed scroll OK')

// Настоящий FixedLayout: отложенный ResizeObserver после remove() не начинает
// раскладку ни разворота, ни ленты. DOM здесь нужен только для конструктора.
const globals = ['HTMLElement', 'CSSStyleSheet', 'ResizeObserver', 'customElements']
const saved = globals.map(key => Object.getOwnPropertyDescriptor(globalThis, key))
try {
  let resize
  let flowReads = 0
  globalThis.HTMLElement = class {
    isConnected = false
    flow = 'paginated'
    attachShadow() { return {} }
    addEventListener() {}
    getAttribute(name) {
      assert.equal(name, 'flow')
      flowReads++
      return this.flow
    }
  }
  globalThis.CSSStyleSheet = class { replaceSync() {} }
  globalThis.ResizeObserver = class {
    constructor(callback) { resize = callback }
    observe() {}
  }
  globalThis.customElements = { define() {} }
  const { FixedLayout } = await import('../../frontend/vendor/foliate-js/fixed-layout.js')
  const el = new FixedLayout()
  for (const flow of ['paginated', 'scrolled']) {
    el.flow = flow
    el.isConnected = true
    resize()
    assert.equal(flowReads, 1)
    flowReads = 0
    el.isConnected = false
    resize()
    el.attributeChangedCallback('zoom', null, 'fit-width')
    assert.equal(flowReads, 0, `${flow}: detached layout must be skipped`)
  }
} finally {
  globals.forEach((key, i) => {
    if (saved[i]) Object.defineProperty(globalThis, key, saved[i])
    else delete globalThis[key]
  })
}
console.log('Detached fixed layout OK')
