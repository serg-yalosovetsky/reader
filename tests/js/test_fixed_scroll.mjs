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
