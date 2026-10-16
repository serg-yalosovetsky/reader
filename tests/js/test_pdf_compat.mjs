// Отсутствующие API эмулируются до импорта, как в старом браузере.
import assert from 'node:assert/strict'
for (const [obj, name] of [
  [Uint8Array.prototype, 'toHex'], [Uint8Array.prototype, 'toBase64'],
  [Uint8Array, 'fromBase64'], [Promise, 'withResolvers'], [Promise, 'try'],
  [Array.prototype, 'findLast'], [Map.prototype, 'getOrInsertComputed'],
  [WeakMap.prototype, 'getOrInsertComputed'], [Set.prototype, 'intersection'],
  [ArrayBuffer.prototype, 'transferToFixedLength'], [globalThis, 'structuredClone'], [URL, 'parse'], [AbortSignal, 'any'],
]) delete obj[name]
await import('../../frontend/vendor/foliate-js/vendor/pdfjs/compat.mjs')
const bytes = new Uint8Array([0, 1, 254, 255])
assert.equal(bytes.toHex(), '0001feff')
assert.deepEqual(Uint8Array.fromBase64(bytes.toBase64()), bytes)
assert.equal(await Promise.try((a, b) => a + b, 2, 3), 5)
await assert.rejects(Promise.try(() => { throw Error('test') }), /test/)
const { promise, resolve } = Promise.withResolvers(); resolve(42); assert.equal(await promise, 42)
assert.equal([1, 2, 3].findLast(v => v % 2), 3)
assert.equal(new Map().getOrInsertComputed('a', () => 7), 7)
assert.deepEqual([...new Set([1, 2]).intersection(new Set([2, 3]))], [2])
assert.deepEqual([...new Uint8Array(bytes.buffer.transferToFixedLength(2))], [0, 1])
const graph = new Map(); graph.set('self', graph)
assert.equal(structuredClone(graph).get('self').get('self') instanceof Map, true)
console.log('PDF compatibility OK')

assert.equal(URL.parse('not a URL'), null)
assert.equal(URL.parse('/a', 'https://example.org').pathname, '/a')
const ac = new AbortController(); const signal = AbortSignal.any([ac.signal]); ac.abort('stop'); assert.equal(signal.reason, 'stop')
