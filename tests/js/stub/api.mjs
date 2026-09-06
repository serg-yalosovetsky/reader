// Заглушка сети: считает запросы и отвечает так же, как /api/translate.
export const calls = []
export let reply = null
export function setReply(fn) { reply = fn }
export const api = {
  async post(url, body) {
    calls.push({ url, items: body.items.map(i => i.text) })
    return reply(body)
  },
}
