// Восстановление позиции чтения — устойчивое к пересборке книги.
//
// Проблема CFI: `epubcfi(...)` кодирует позицию как индекс секции + offset в узле.
// Когда FanFicFare докачивает главы и пересобирает EPUB, индексы секций съезжают —
// старый CFI указывает на другую секцию (симптом: «открылось в начале следующей
// главы»). Доля прочитанного (ratio) переживает пересборку, но мажет грубо.
//
// Решение — текстовый якорь: при каждом релокейте запоминаем первые слова текста
// вверху экрана. При открытии ИЩЕМ этот текст в книге и переходим ровно на него.
// Текст переживает репагинацию; CFI/ratio остаются быстрым/грубым фолбэком.
//
// Гибрид при восстановлении (по убыванию точности):
//   1) text-anchor — ищем сохранённую фразу (сначала в вероятной секции, затем во
//      всей книге) и переходим на неё;
//   2) CFI — если якоря нет (старая запись) или он не нашёлся;
//   3) ratio — грубый кросс-девайс фолбэк (напр. импорт из ReadEra).
import { logErr } from './log.js'

// Сколько слов берём в якорь. Достаточно, чтобы фраза была уникальной в пределах
// книги, но не настолько много, чтобы правка одного слова сломала совпадение.
const ANCHOR_WORDS = 12
// Якорь короче этого (в символах после нормализации) не ищем — слишком неоднозначен.
const MIN_ANCHOR_LEN = 8

const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim()

// Текст видимого диапазона → якорная фраза (первые ANCHOR_WORDS слов сверху экрана).
export function captureAnchor(range) {
  if (!range) return ''
  const raw = normalize(range.toString())
  if (!raw) return ''
  return raw.split(' ').slice(0, ANCHOR_WORDS).join(' ')
}

// Карта нормализованного текста узла-корня → позиции (узел, offset) для каждого
// символа. Пробелы схлопываются в один, регистр приводится к нижнему — так якорь
// совпадёт, даже если пересборка книги слегка изменила отступы/переносы.
function buildTextMap(root) {
  const doc = root.ownerDocument
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let norm = ''
  const pos = [] // pos[i] = { node, offset } для символа norm[i]
  let lastWasSpace = true
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const t = node.nodeValue
    for (let k = 0; k < t.length; k++) {
      const ch = t[k]
      if (/\s/.test(ch)) {
        if (lastWasSpace) continue
        norm += ' '
        pos.push({ node, offset: k })
        lastWasSpace = true
      } else {
        norm += ch.toLowerCase()
        pos.push({ node, offset: k })
        lastWasSpace = false
      }
    }
  }
  return { norm, pos }
}

// Найти якорную фразу в документе секции → DOM-Range (или null).
function findRange(root, anchor) {
  if (!root) return null
  const needle = normalize(anchor).toLowerCase()
  if (needle.length < MIN_ANCHOR_LEN) return null
  const { norm, pos } = buildTextMap(root)
  const at = norm.indexOf(needle)
  if (at < 0) return null
  const start = pos[at]
  const end = pos[at + needle.length - 1]
  if (!start || !end) return null
  const range = root.ownerDocument.createRange()
  range.setStart(start.node, start.offset)
  range.setEnd(end.node, end.offset + 1)
  return range
}

// Порядок обхода секций: от вероятной (hint) наружу — [h, h-1, h+1, h-2, h+2, …].
// hint берём из доли прочитанного; при пересборке он неточен, но резко ускоряет
// типичный случай (книга не менялась) и не мешает найти сдвинутый текст.
function sectionOrder(n, hint) {
  const h = Math.max(0, Math.min(n - 1, hint | 0))
  const order = [h]
  for (let d = 1; d < n; d++) {
    if (h - d >= 0) order.push(h - d)
    if (h + d < n) order.push(h + d)
  }
  return order
}

// Ищем якорь по секциям (createDocument — копия DOM секции, как во внутреннем
// поиске foliate) и строим CFI найденного места. Свой поиск, а не view.search(),
// чтобы не оставлять overlay-подсветки результатов на странице.
async function findAnchorCfi(view, anchor, hint) {
  const sections = view?.book?.sections || []
  if (!sections.length) return null
  for (const i of sectionOrder(sections.length, hint)) {
    const sec = sections[i]
    if (!sec?.createDocument) continue
    let doc
    try {
      doc = await sec.createDocument()
    } catch {
      continue
    }
    const range = findRange(doc.body || doc.documentElement, anchor)
    if (range) {
      try {
        return view.getCFI(i, range)
      } catch (e) {
        logErr('getCFI for anchor failed', e)
      }
    }
  }
  return null
}

// Поздний reflow (веб-шрифты @import, докачка картинок) наращивает контент над
// якорем, и позиция «сползает». Ждём готовности шрифтов/картинок текущей секции
// и повторно доезжаем до цели по устоявшейся раскладке.
async function settleAndReanchor(view, target) {
  try {
    const doc = view?.renderer?.getContents?.()?.[0]?.doc
    if (!doc) return
    const waits = []
    if (doc.fonts?.ready) waits.push(doc.fonts.ready.catch(() => {}))
    for (const im of [...doc.images]) {
      if (im.complete) continue
      waits.push(new Promise((res) => {
        im.addEventListener('load', res, { once: true })
        im.addEventListener('error', res, { once: true })
      }))
    }
    // Не зависаем дольше 2.5с (битые/долгие картинки).
    await Promise.race([Promise.all(waits), new Promise((r) => setTimeout(r, 2500))])
    await view.goTo(target)
  } catch {}
}

// Восстановить позицию при открытии книги: якорь → CFI → доля → начало.
// view уже .open()-нут (book.sections доступны), но ещё не .init()-нут (ничего
// не отрендерено) — здесь и выбираем, куда рендерить первый кадр.
export async function restorePosition(view, prog) {
  const sections = view?.book?.sections || []
  const anchor = prog?.text_anchor || ''
  const ratio = prog?.ratio || 0

  let target = null
  if (anchor && sections.length) {
    const hint = ratio > 0 ? Math.round(ratio * sections.length) : 0
    target = await findAnchorCfi(view, anchor, hint)
  }
  // Старая запись без якоря — пробуем сохранённый CFI как основной таргет.
  if (!target && prog?.locator) target = prog.locator

  if (target) {
    try {
      await view.init({ lastLocation: target })
      await settleAndReanchor(view, target)
      return
    } catch (e) {
      logErr('restore by target failed, fallback to ratio', e)
      // view мог упасть на этапе якоря — секция уже отрендерена, доедем по доле.
    }
  } else {
    try {
      await view.init({ showTextStart: true })
    } catch (e) {
      logErr('init showTextStart failed', e)
    }
  }

  if (ratio > 0) {
    try {
      await view.goToFraction(ratio)
    } catch {}
  }
}
