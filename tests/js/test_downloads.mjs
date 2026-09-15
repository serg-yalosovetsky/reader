// Панель «Скачивания»: тексты статусов, прогресс и экранирование (serg/tasks#893).
//
// Запуск: node tests/js/test_downloads.mjs
// Проверяется НАСТОЯЩИЙ frontend/js/downloads.js; DOM и сеть — заглушки
// (tests/js/hooks.mjs подменяет импорты). Название книги приходит с чужого сайта,
// поэтому экранирование проверяется явно.
import { register } from 'node:module'
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

register('./hooks.mjs', import.meta.url)

const dl = await import(
  pathToFileURL(new URL('../../frontend/js/downloads.js', import.meta.url).pathname).href
)

const running = {
  job_id: 'a', status: 'running', title: 'Великие Спящие',
  query: 'https://readli.net/chitat-online/?b=1', elapsed_s: 75, interruptions: 0,
  progress: { done: 30, total: 163, unit: 'pages', stage: 'скачивание' }, work: null, error: null,
}

let d = dl.describeJob(running)
assert.equal(d.title, 'Великие Спящие')
assert.match(d.sub, /скачивание/)
assert.match(d.sub, /30 из 163 стр\./)
assert.match(d.sub, /1:15/)
assert.equal(d.pct, 18)

d = dl.describeJob({ ...running, progress: { done: 12, total: null, unit: 'requests', stage: 'скачивание' } })
assert.match(d.sub, /запросов: 12/)
assert.equal(d.pct, null)

d = dl.describeJob({ ...running, interruptions: 1 })
assert.match(d.sub, /возобновлено после рестарта/)

d = dl.describeJob({
  job_id: 'b', status: 'queued', title: '', query: 'https://***@ficbook.net/readfic/1',
  interruptions: 1, interrupted_by: 'shutdown', progress: { done: 0, total: null, unit: '', stage: '' },
})
assert.equal(d.title, 'ficbook.net/readfic/1')
assert.match(d.sub, /в очереди/)
assert.match(d.sub, /продолжится после рестарта/)

d = dl.describeJob({
  job_id: 'c', status: 'done', title: 'Червь', elapsed_s: 425,
  work: { id: 3693, title: 'Червь', chapters: 311 }, progress: { done: 311, total: 311, unit: 'chapters', stage: '' },
})
assert.match(d.sub, /готово/)
assert.match(d.sub, /311 гл\./)
assert.match(d.sub, /7:05/)
assert.equal(d.pct, null)

d = dl.describeJob({
  job_id: 'd', status: 'error', title: '', query: 'https://ficbook.net/readfic/0',
  error: 'Не удалось скачать: Story does not exist', progress: {},
})
assert.equal(d.kind, 'error')
assert.match(d.sub, /Story does not exist/)

assert.equal(dl.formatElapsed(3723), '1:02:03')
assert.equal(dl.formatElapsed(null), '')

const html = dl.renderJobsHtml([{ ...running, title: '<img src=x onerror=alert(1)>' }])
assert.ok(!html.includes('<img'), 'название книги должно экранироваться')
assert.match(html, /&lt;img/)
assert.match(html, /width:18%/)

assert.equal(dl.countActive([running, { status: 'queued' }, { status: 'done' }, { status: 'error' }]), 2)

// Скрытая вкладка пропускает только повторные опросы: первый запрос при загрузке
// и запрос после «Добавить» идут всегда (иначе фоновая вкладка оставалась с пустой панелью).
assert.equal(dl.shouldPoll('hidden', true), true)
assert.equal(dl.shouldPoll('hidden', false), false)
assert.equal(dl.shouldPoll('visible', false), true)

console.log('ok: downloads panel')
