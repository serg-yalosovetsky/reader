// Восстановление позиции чтения.
//
// Порядок (serg/tasks#1853):
//   1) CFI — если файл книги тот же, в котором его посчитали (book_rev = SHA-1
//      файла). Это точная координата, никакого поиска;
//   2) текстовый якорь — если книгу пересобрали (FanFicFare докачал главы, CFI
//      съехал на другую секцию). Ищем сначала в секции из CFI, затем в главе с
//      сохранённым названием, затем по соседству; внутри секции — вхождение,
//      ближайшее к CFI, а не первое. Сначала полным якорем, потом коротким;
//   3) CFI как есть; 4) доля — грубый фолбэк (импорт из ReadEra).
//
// Раньше якорь шёл первым и брался первым вхождением от секции «доля × число
// секций». У фанфиков верх страницы — часто авторская плашка, одинаковая в
// каждой главе («Анонсы новых глав…» — в 14 секциях из 155), и книга
// открывалась на две главы раньше.
import { logErr } from './log.js'

// Сколько слов берём в якорь. Ищем сперва полным (24 слова — почти всегда
// уникален), потом первыми SHORT_WORDS: правка одного слова в хвосте фразы не
// должна ломать совпадение, и старые записи хранят ровно 12 слов.
const ANCHOR_WORDS = 24
const SHORT_WORDS = 12
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

// Все вхождения якоря в документ секции → позиции в нормализованном тексте.
function findAll(norm, needle) {
  const hits = []
  for (let at = norm.indexOf(needle); at >= 0; at = norm.indexOf(needle, at + 1)) hits.push(at)
  return hits
}

// Позиция начала DOM-Range в нормализованном тексте (для выбора ближайшего
// вхождения к месту из CFI). -1 — не нашли.
function offsetOf(pos, range) {
  if (!range) return -1
  const node = range.startContainer
  for (let i = 0; i < pos.length; i++) {
    if (pos[i].node === node && pos[i].offset >= range.startOffset) return i
    if (pos[i].node !== node && node.nodeType === 1 && node.contains?.(pos[i].node)) return i
  }
  return -1
}

// Найти якорную фразу в документе секции → DOM-Range (или null). near — Range
// места из CFI в этой же секции: из нескольких вхождений берём ближайшее к нему.
function findRange(root, needle, near) {
  if (!root || needle.length < MIN_ANCHOR_LEN) return null
  const { norm, pos } = buildTextMap(root)
  const hits = findAll(norm, needle)
  if (!hits.length) return null
  let at = hits[0]
  if (hits.length > 1 && near) {
    const target = offsetOf(pos, near)
    if (target >= 0) at = hits.reduce((b, h) => (Math.abs(h - target) < Math.abs(b - target) ? h : b))
  }
  const start = pos[at]
  const end = pos[at + needle.length - 1]
  if (!start || !end) return null
  const range = root.ownerDocument.createRange()
  range.setStart(start.node, start.offset)
  range.setEnd(end.node, end.offset + 1)
  return range
}

// Порядок обхода секций: сперва подсказки (секция из CFI, секции главы с
// сохранённым названием), потом от первой подсказки наружу.
function sectionOrder(n, hints) {
  const seen = new Set()
  const order = []
  const add = (i) => { if (i >= 0 && i < n && !seen.has(i)) { seen.add(i); order.push(i) } }
  hints.forEach(add)
  const h = hints.length ? hints[0] : 0
  add(h)
  for (let d = 1; d < n; d++) { add(h - d); add(h + d) }
  return order
}

function flatToc(items, out = []) {
  for (const it of items || []) { out.push(it); flatToc(it.subitems, out) }
  return out
}

// Подсказки, где искать: секция из CFI, секции глав с тем же названием, доля.
function sectionHints(view, pos) {
  const n = view?.book?.sections?.length || 0
  const hints = []
  let cfiIndex = -1
  if (pos?.locator) {
    try { cfiIndex = view.resolveCFI(pos.locator)?.index ?? -1 } catch { /* битый CFI */ }
    if (cfiIndex >= 0) hints.push(cfiIndex)
  }
  if (pos?.chapter) {
    for (const it of flatToc(view?.book?.toc)) {
      if ((it.label || '').trim() !== pos.chapter.trim() || !it.href) continue
      try {
        const idx = view.resolveNavigation(it.href)?.index
        if (typeof idx === 'number') hints.push(idx)
      } catch { /* битая ссылка оглавления */ }
    }
  }
  if (pos?.ratio > 0 && n) hints.push(Math.min(n - 1, Math.round(pos.ratio * n)))
  return { hints, cfiIndex }
}

// Ищем якорь по секциям (createDocument — копия DOM секции, как во внутреннем
// поиске foliate) и строим CFI найденного места. Свой поиск, а не view.search(),
// чтобы не оставлять overlay-подсветки результатов на странице.
async function findAnchorCfi(view, pos) {
  const sections = view?.book?.sections || []
  const anchor = normalize(pos?.text_anchor).toLowerCase()
  if (!sections.length || anchor.length < MIN_ANCHOR_LEN) return null
  const { hints, cfiIndex } = sectionHints(view, pos)
  const words = anchor.split(' ')
  const needles = [anchor]
  if (words.length > SHORT_WORDS) needles.push(words.slice(0, SHORT_WORDS).join(' '))
  // Документы секций не кешируем: на телефоне 150+ DOM-копий — это память, а
  // второй (короткий) проход нужен, только если полный якорь не нашёлся нигде.
  const docOf = async (i) => {
    try { return await sections[i]?.createDocument?.() } catch { return null }
  }
  const order = sectionOrder(sections.length, hints)
  for (const needle of needles) {
    for (const i of order) {
      const doc = await docOf(i)
      if (!doc) continue
      let near = null
      if (i === cfiIndex) {
        try { near = view.resolveCFI(pos.locator).anchor(doc) } catch { /* CFI не ложится на новую разметку */ }
      }
      const range = findRange(doc.body || doc.documentElement, needle, near)
      if (range) {
        try {
          return view.getCFI(i, range)
        } catch (e) {
          logErr('getCFI for anchor failed', e)
        }
      }
    }
  }
  return null
}

// У старых PDF foliate пишет искусственный CFI без перехода внутрь документа.
// EPUB содержит «!». Проверяем это только для пары PDF/EPUB, не для FB2.
export function positionForFormat(pos, format = '') {
  const saved = pos?.file_format || (/^epubcfi\(/.test(pos?.locator || '')
    ? (pos.locator.includes('!') ? 'epub' : 'pdf') : '')
  if (format && saved && format !== saved && [format, saved].includes('pdf')) {
    return { ratio: pos?.ratio || 0 }
  }
  return pos
}

// resolveNavigation и goTo в foliate могут проглотить ошибку без исключения.
async function validTarget(view, target) {
  try {
    const resolved = await view.resolveNavigation(target)
    if (!resolved || !Number.isInteger(resolved.index)
      || resolved.index < 0 || resolved.index >= view.book.sections.length) return false
    const section = view.book.sections[resolved.index]
    if (typeof resolved.anchor === 'function' && section.createDocument) {
      // Секция может существовать, а путь CFI внутри неё — уже нет.
      const doc = await section.createDocument()
      if (!resolved.anchor(doc)) return false
    }
    return true
  } catch { return false }
}

// Куда ехать: CFI при том же файле, иначе якорь, иначе CFI как есть.
async function pickTarget(view, pos, rev) {
  // Искусственный PDF-CFI хранит страницу; доля ленты хранит и смещение.
  if (view?.isFixedLayout && view.renderer?.scrolled && Number.isFinite(pos?.ratio)) {
    return { fraction: pos.ratio }
  }
  if (pos?.locator && pos.book_rev && rev && pos.book_rev === rev) return pos.locator
  const byAnchor = pos?.text_anchor ? await findAnchorCfi(view, pos) : null
  return byAnchor || pos?.locator || null
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
      // Ленивые картинки за пределами экрана не начинают грузиться вообще, и их
      // события не наступают никогда — ожидание таких всегда упиралось в таймаут
      // ниже и стоило 2.5 с на ровном месте. Ждём только те, что реально грузятся.
      if (im.loading === 'lazy') continue
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

// Перейти к сохранённой позиции в УЖЕ открытой книге (история переходов,
// serg/tasks#1078). Тот же порядок точности, что и при открытии. Возвращает
// true, если куда-то доехали: провал должен быть видимым — иначе нажатие
// «Назад» выглядит как ничего не делающее.
export async function gotoPosition(view, pos, rev = '', format = '') {
  pos = positionForFormat(pos, format)
  const ratio = pos?.ratio || 0
  const target = await pickTarget(view, pos, rev)
  if (target && await validTarget(view, target)) {
    try {
      if (!await view.goTo(target)) throw new Error('locator не разрешился')
      await settleAndReanchor(view, target)
      return true
    } catch (e) {
      logErr('переход к сохранённой позиции не удался, иду по доле', e)
    }
  }
  if (ratio > 0) {
    try {
      await view.goToFraction(ratio)
      return true
    } catch (e) {
      logErr('переход по доле не удался', e)
    }
  }
  return false
}

// Восстановить позицию при открытии книги. rev — SHA-1 открытого файла.
// view уже .open()-нут (book.sections доступны), но ещё не .init()-нут (ничего
// не отрендерено) — здесь и выбираем, куда рендерить первый кадр.
export async function restorePosition(view, prog, rev = '', format = '') {
  prog = positionForFormat(prog, format)
  const ratio = prog?.ratio || 0
  const target = await pickTarget(view, prog, rev)

  if (target && await validTarget(view, target)) {
    try {
      await view.init({ lastLocation: target })
      await settleAndReanchor(view, target)
      return true
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
      return true
    } catch {}
    return false
  }
  return true
}

// SHA-1 файла книги (hex) — ревизия, к которой привязан CFI. Пусто, если
// SubtleCrypto недоступен (не https): тогда восстановление идёт по якорю.
export async function bookRev(blob) {
  try {
    const buf = await blob.arrayBuffer()
    const dig = await crypto.subtle.digest('SHA-1', buf)
    return [...new Uint8Array(dig)].map((b) => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return ''
  }
}
