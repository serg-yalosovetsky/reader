// Совместимость pdf.js 5.5.207. Импортируется отдельно страницей и воркером.
// Нативные реализации не заменяем. При обновлении сборки повторить аудит API.
const define = (target, name, value) => {
  if (typeof target[name] !== 'function')
    Object.defineProperty(target, name, { value, writable: true, configurable: true })
}
define(Uint8Array.prototype, 'toHex', function () {
  return Array.from(this, byte => byte.toString(16).padStart(2, '0')).join('')
})
define(Uint8Array.prototype, 'toBase64', function (options = {}) {
  let binary = ''
  for (let i = 0; i < this.length; i += 8192)
    binary += String.fromCharCode(...this.subarray(i, i + 8192))
  let result = btoa(binary)
  if (options.alphabet === 'base64url') result = result.replace(/\+/g, '-').replace(/\//g, '_')
  return options.omitPadding ? result.replace(/=+$/, '') : result
})
define(Uint8Array, 'fromBase64', function (text, options = {}) {
  if (options.alphabet === 'base64url') text = text.replace(/-/g, '+').replace(/_/g, '/')
  return Uint8Array.from(atob(text), char => char.charCodeAt(0))
})
define(Promise, 'withResolvers', function () {
  let resolve, reject
  const promise = new this((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
})
define(Promise, 'try', function (fn, ...args) {
  return new this(resolve => resolve(fn(...args)))
})
define(Array.prototype, 'findLast', function (fn, thisArg) {
  for (let i = this.length - 1; i >= 0; i--)
    if (fn.call(thisArg, this[i], i, this)) return this[i]
})
define(Set.prototype, 'intersection', function (other) {
  const result = new Set()
  const source = this.size <= other.size ? this : other
  const target = source === this ? other : this
  for (const value of source.keys()) if (target.has(value)) result.add(value)
  return result
})
// pdf.js передаёт обычные буферы; slice сохраняет нужный диапазон и длину.
// Отсоединяем оригинал через structuredClone, если браузер это умеет.
define(ArrayBuffer.prototype, 'transferToFixedLength', function (length = this.byteLength) {
  const result = new ArrayBuffer(length)
  new Uint8Array(result).set(new Uint8Array(this, 0, Math.min(length, this.byteLength)))
  if (globalThis.structuredClone) structuredClone(this, { transfer: [this] })
  return result
})
// В старых браузерах копируем граф объектов без transfer-оптимизации.
// Реальный Worker по-прежнему использует нативный postMessage/transfer.
if (!globalThis.structuredClone) {
  // PerformanceMark использует тот же алгоритм сериализации, в том числе для
  // ImageBitmap. Он появился раньше глобального structuredClone.
  let nativeClone
  try {
    const probe = new Map([['ok', true]])
    if (new PerformanceMark('pdf-clone', { detail: probe }).detail.get('ok'))
      nativeClone = value => new PerformanceMark('pdf-clone', { detail: value }).detail
  } catch {}
  globalThis.structuredClone = nativeClone || function clone(value, options, seen = new Map()) {
    if (value === null || typeof value !== 'object') {
      if (typeof value === 'function' || typeof value === 'symbol')
        throw new DOMException('Объект нельзя клонировать', 'DataCloneError')
      return value
    }
    if (seen.has(value)) return seen.get(value)
    if (value instanceof ArrayBuffer) {
      const copy = value.slice(0); seen.set(value, copy); return copy
    }
    if (ArrayBuffer.isView(value)) {
      const buffer = clone(value.buffer, undefined, seen)
      return value instanceof DataView
        ? new DataView(buffer, value.byteOffset, value.byteLength)
        : new value.constructor(buffer, value.byteOffset, value.length)
    }
    if (value instanceof Date) return new Date(value)
    if (value instanceof RegExp) return new RegExp(value)
    if (typeof Blob !== 'undefined' && value instanceof Blob) return value.slice(0, value.size, value.type)
    const copy = value instanceof Map ? new Map() : value instanceof Set ? new Set() : Array.isArray(value) ? [] : {}
    seen.set(value, copy)
    if (value instanceof Map) for (const [k, v] of value) copy.set(clone(k, undefined, seen), clone(v, undefined, seen))
    else if (value instanceof Set) for (const v of value) copy.add(clone(v, undefined, seen))
    else for (const k of Object.keys(value)) Object.defineProperty(copy, k, { value: clone(value[k], undefined, seen), enumerable: true, writable: true, configurable: true })
    return copy
  }
}
for (const prototype of [Map.prototype, WeakMap.prototype]) {
  define(prototype, 'getOrInsertComputed', function (key, callback) {
    if (this.has(key)) return this.get(key)
    const value = callback(key)
    this.set(key, value)
    return value
  })
}
for (const prototype of [Array.prototype, String.prototype, Object.getPrototypeOf(Uint8Array.prototype)]) {
  define(prototype, 'at', function (index) {
    index = Math.trunc(index) || 0
    return this[index < 0 ? this.length + index : index]
  })
}
define(URL, 'parse', function (url, base) {
  try { return new URL(url, base) } catch { return null }
})
define(AbortSignal, 'any', function (signals) {
  const controller = new AbortController()
  const listeners = new Map()
  const abort = signal => {
    controller.abort(signal.reason)
    for (const [s, fn] of listeners) s.removeEventListener('abort', fn)
    listeners.clear()
  }
  for (const signal of signals) {
    if (signal.aborted) { abort(signal); break }
    const fn = () => abort(signal)
    listeners.set(signal, fn)
    signal.addEventListener('abort', fn, { once: true })
  }
  return controller.signal
})
