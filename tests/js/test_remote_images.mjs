import assert from 'node:assert/strict'
import { deferRemoteImages, restoreRemoteImages, protectRemoteImages } from '../../frontend/js/core/remote-images.js'

const input = `<html><img width="100" height="80" style="height:80px" src="https://host/a?a=1&amp;b=2" srcset="local.jpg 1x, http://host/b 2x"/><svg><image xlink:href='https://host/c'/></svg><img src=HTTP://host/d><img src="images/local.png"><img src="data:image/png;base64,AAA"></html>`
const output = deferRemoteImages(input)
assert.ok(output.includes('data-remote-src="https://host/a?a=1&amp;b=2"'))
assert.ok(output.includes('data-remote-srcset="local.jpg 1x, http://host/b 2x"'))
assert.ok(output.includes("data-remote-xlink-href='https://host/c'"))
assert.ok(output.includes('data-remote-src="HTTP://host/d"'))
assert.ok(output.includes('width="100" height="80" style="height:80px"'))
assert.ok(output.includes('<img src="images/local.png">'))
assert.ok(output.includes('<img src="data:image/png;base64,AAA">'))
assert.equal(deferRemoteImages(output), output)
assert.equal(deferRemoteImages('<p>Текст</p>'), '<p>Текст</p>')
const attrs = new Map([['data-remote-src', 'https://host/a'], ['width', '100']])
const el = { hasAttribute: key => attrs.has(key), getAttribute: key => attrs.get(key),
  setAttribute: (key, value) => attrs.set(key, value), removeAttribute: key => attrs.delete(key) }
restoreRemoteImages({ querySelectorAll: () => [el] })
assert.equal(attrs.get('loading'), 'lazy')
assert.equal(attrs.get('src'), 'https://host/a')
assert.equal(attrs.get('width'), '100')
assert.equal(attrs.has('data-remote-src'), false)
let listener
assert.equal(protectRemoteImages({ transformTarget: { addEventListener: (_, fn) => { listener = fn } } }), true)
const detail = { type: 'application/xhtml+xml', data: Promise.resolve(input) }
listener({ detail })
assert.equal(await detail.data, output)
const css = { type: 'text/css', data: 'body{}' }
listener({ detail: css })
assert.equal(css.data, 'body{}')
assert.equal(protectRemoteImages({ rendition: { layout: 'pre-paginated' } }), false)
console.log('remote-images: OK')

// Кавычки и символ > внутри атрибутов не должны путать переписывание.
const tricky = `<img alt='text src="https://keep/" >' src="https://defer/a" width="40">`
assert.ok(deferRemoteImages(tricky).includes(`alt='text src="https://keep/" >'`))
assert.ok(deferRemoteImages(tricky).includes('data-remote-src="https://defer/a"'))
