// Слои: открытая модалка (.modal-overlay — «Аккаунты», «Из статей») должна
// перекрывать кнопку и панель «Скачивания» (serg/tasks#893). Все трое —
// position: fixed без изолирующего предка, так что порядок решает z-index.
import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'

const css = readFileSync(new URL('../../frontend/css/theme.css', import.meta.url), 'utf8')

function zIndex(selector) {
  const esc = selector.replace(/[.#]/g, (c) => '\\' + c)
  const re = new RegExp(`(?:^|[\\n}])\\s*${esc}\\s*\\{([^}]*)\\}`)
  const m = css.match(re)
  assert.ok(m, `нет правила ${selector} в theme.css`)
  const z = m[1].match(/z-index:\s*(\d+)/)
  assert.ok(z, `у ${selector} нет z-index`)
  return Number(z[1])
}

const modal = zIndex('.modal-overlay')
for (const sel of ['.dl-toggle', '.dl-panel']) {
  const z = zIndex(sel)
  assert.ok(modal > z, `.modal-overlay (${modal}) должна быть выше ${sel} (${z})`)
}
console.log('ok: z-order modal над панелью скачиваний')
