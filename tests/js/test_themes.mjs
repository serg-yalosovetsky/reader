// Темы читалки (serg/tasks#911): чёрная с зелёным текстом + согласованность.
//
// Запуск: node tests/js/test_themes.mjs
// Тема регистрируется в пяти местах: палитра в css/theme.css, свотч в index.html,
// цикл кнопки библиотеки (LIB_THEMES) и два списка тёмных тем — в library.js и
// reader-core.js (по нему iframe книги получает color-scheme). Забытое место не
// видно глазом сразу: тёмная тема без записи в isDark рисует книгу со светлой
// схемой — светлые полосы прокрутки и поля ввода на чёрном фоне.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (p) => readFileSync(new URL('../../frontend/' + p, import.meta.url), 'utf8')
const css = read('css/theme.css')
const html = read('index.html')
const lib = read('js/library.js')
const core = read('js/reader-core.js')

// --- Палитры из CSS ----------------------------------------------------------
const themes = {}
for (const m of css.matchAll(/html\[data-theme="([\w-]+)"\]\s*\{([^}]*)\}/g)) {
  const vars = {}
  for (const d of m[2].matchAll(/(--[\w-]+|color-scheme)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].trim()
  themes[m[1]] = vars
}
const names = Object.keys(themes).sort()
assert.ok(names.length >= 7, `палитры не разобрались из theme.css: ${names}`)

// --- WCAG-контраст -----------------------------------------------------------
function rgb(hex) {
  let h = hex.replace('#', '')
  if (h.length === 3) h = [...h].map((c) => c + c).join('')
  assert.match(h, /^[0-9a-f]{6}$/i, `не hex-цвет: ${hex}`)
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16))
}
function luminance(hex) {
  const [r, g, b] = rgb(hex).map((v) => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

// --- 1. Чёрная тема с зелёным текстом ----------------------------------------
const t = themes.phosphor
assert.ok(t, 'нет темы phosphor (чёрный фон, зелёный текст) в css/theme.css')
assert.equal(t['color-scheme'], 'dark', 'phosphor: color-scheme должен быть dark')
assert.ok(luminance(t['--bg']) < 0.002, `phosphor: фон не чёрный (${t['--bg']})`)
const [r, g, b] = rgb(t['--fg'])
assert.ok(g > 1.5 * Math.max(r, b), `phosphor: текст не зелёный (${t['--fg']})`)
const report = {
  'текст/фон': contrast(t['--fg'], t['--bg']),
  'текст/карточка': contrast(t['--fg'], t['--card']),
  'текст/мягкий фон': contrast(t['--fg'], t['--bg-soft']),
  'приглушённый/фон': contrast(t['--fg-dim'], t['--bg']),
  'акцент/фон': contrast(t['--accent'], t['--bg']),
  'текст на акценте': contrast(t['--accent-text'], t['--accent']),
}
// Длинное чтение — AAA (7:1); подписи и ссылки — AA (4.5:1).
assert.ok(report['текст/фон'] >= 7, `phosphor: текст/фон ${report['текст/фон'].toFixed(2)} < 7`)
assert.ok(report['текст/карточка'] >= 7, `phosphor: текст/карточка ${report['текст/карточка'].toFixed(2)} < 7`)
assert.ok(report['текст/мягкий фон'] >= 7, `phosphor: текст/мягкий фон ${report['текст/мягкий фон'].toFixed(2)} < 7`)
assert.ok(report['приглушённый/фон'] >= 4.5, `phosphor: приглушённый ${report['приглушённый/фон'].toFixed(2)} < 4.5`)
assert.ok(report['акцент/фон'] >= 4.5, `phosphor: акцент/фон ${report['акцент/фон'].toFixed(2)} < 4.5`)
assert.ok(report['текст на акценте'] >= 4.5, `phosphor: текст на акценте ${report['текст на акценте'].toFixed(2)} < 4.5`)

// --- 2. Каждая палитра зарегистрирована везде --------------------------------
const set = (a) => [...new Set(a)].sort()
const swatches = set([...html.matchAll(/class="swatch"\s+data-theme="([\w-]+)"/g)].map((m) => m[1]))
assert.deepEqual(swatches, names, 'свотчи в index.html не совпадают с палитрами theme.css')

const libThemes = lib.match(/const LIB_THEMES = \[([^\]]*)\]/)
assert.ok(libThemes, 'не найден LIB_THEMES в library.js')
assert.deepEqual(set(libThemes[1].match(/[\w-]+/g)), names, 'LIB_THEMES не совпадает с палитрами theme.css')

const dark = names.filter((n) => themes[n]['color-scheme'] === 'dark')
for (const [file, src] of [['library.js', lib], ['reader-core.js', core]]) {
  const lists = [...src.matchAll(/\[([^\]]*)\]\.includes\(prefs\.theme\)/g)]
  assert.ok(lists.length > 0, `${file}: не найден список тёмных тем`)
  for (const l of lists) assert.deepEqual(set(l[1].match(/[\w-]+/g)), dark, `${file}: список тёмных тем не совпадает с color-scheme: dark`)
}

console.log('ok: темы —', names.join(', '))
console.log('phosphor контраст:', Object.entries(report).map(([k, v]) => `${k} ${v.toFixed(2)}`).join('; '))
