// Поиск по книге (foliate view.search).
import { $, escapeHtml } from './core/dom.js'
import { view } from './core/state.js'
import { closePanels } from './navigation.js'

// ===================== Поиск по книге =====================
// foliate view.search() — асинхронный генератор: по секциям выдаёт совпадения
// (cfi + excerpt {pre,match,post}) и прогресс; сам подсвечивает их в тексте.
let searchSeq = 0
$('#search-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  if (!view) return
  const q = $('#search-input').value.trim()
  const results = $('#search-results'); results.innerHTML = ''
  const meta = $('#search-meta')
  view.clearSearch?.()
  if (!q) { meta.textContent = ''; return }
  const seq = ++searchSeq // отменяем результаты прошлого запроса
  // OR-поиск: «слово1|слово2» → несколько последовательных запросов.
  const terms = q.split('|').map(t => t.trim()).filter(Boolean)
  const isMulti = terms.length > 1
  meta.textContent = isMulti ? `Поиск по ${terms.length} словам…` : 'Поиск…'
  let count = 0
  try {
    for (const term of terms) {
      if (seq !== searchSeq) return
      for await (const r of view.search({ query: term })) {
        if (seq !== searchSeq) return
        if (r === 'done') break
        if (r.subitems) {
          for (const sub of r.subitems) {
            count++
            const lbl = isMulti ? ((r.label ? r.label + ' ' : '') + '[' + term + ']') : r.label
            results.append(searchResult(lbl, sub))
          }
          meta.textContent = `Найдено: ${count}`
        } else if (typeof r.progress === 'number') {
          meta.textContent = isMulti
            ? `«${term}»: ${Math.round(r.progress * 100)}% (всего ${count})`
            : `Поиск… ${Math.round(r.progress * 100)}% (найдено ${count})`
        }
      }
    }
    if (seq === searchSeq) meta.textContent = count ? `Найдено совпадений: ${count}` : 'Ничего не найдено'
  } catch (err) {
    if (seq === searchSeq) meta.textContent = 'Ошибка поиска: ' + (err?.message || '')
  }
})

function searchResult(label, sub) {
  const ex = sub.excerpt || {}
  const a = document.createElement('a')
  a.className = 'search-result'; a.href = '#'
  a.innerHTML =
    (label ? `<span class="sr-label">${escapeHtml(label)}</span>` : '') +
    `<span class="sr-ex">${escapeHtml(ex.pre || '')}<mark>${escapeHtml(ex.match || '')}</mark>${escapeHtml(ex.post || '')}</span>`
  a.addEventListener('click', (ev) => { ev.preventDefault(); view.goTo(sub.cfi); closePanels() })
  return a
}
