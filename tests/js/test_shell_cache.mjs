// SW должен различать версии модулей и сохранять полный URL в офлайн-кэше.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
const handlers = {}
const entries = new Map()
const removed = []
let offline = false
const cache = {
  put: async (req, resp) => entries.set(req.url, resp),
  match: async req => entries.get(req.url),
}
const self = {
  location: { origin: 'https://reader.test' },
  clients: { claim: async () => {} },
  addEventListener: (name, fn) => { handlers[name] = fn },
}
const context = vm.createContext({ self, URL, Response, setTimeout, clearTimeout,
  caches: { open: async name => { assert.equal(name, 'reader-shell-v13'); return cache },
    keys: async () => ['reader-shell-v12', 'reader-shell-v13', 'reader-books-v1'],
    delete: async name => removed.push(name) },
  fetch: async req => {
    if (offline) throw Error('нет сети')
    return { ok: true, status: 200, type: 'basic', redirected: false,
      url: req.url, clone() { return this } }
  },
})
vm.runInContext(readFileSync(new URL('../../frontend/sw.js', import.meta.url), 'utf8'), context)
let activation
handlers.activate({ waitUntil: promise => { activation = promise } })
await activation
assert.deepEqual(removed, ['reader-shell-v12'])
async function request(path) {
  let response
  handlers.fetch({ request: { url: 'https://reader.test' + path, method: 'GET', mode: 'cors' },
    respondWith: promise => { response = promise } })
  return await response
}
for (const module of ['pdf.mjs', 'pdf.worker.mjs', 'compat.mjs']) {
  const path = `/vendor/foliate-js/vendor/pdfjs/${module}?v=20261016-1`
  const fresh = await request(path)
  assert.equal(fresh.url, 'https://reader.test' + path)
  offline = true
  assert.equal(await request(path), fresh)
  assert.equal((await request(path.replace('20261016-1', 'old'))).status, 503)
  offline = false
}
console.log('ok: SW версии модулей и удаление старой оболочки')
