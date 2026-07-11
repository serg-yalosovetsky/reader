// Инлайн-картинки: ссылки на изображения в тексте книги (примечания фанфиков со
// ссылками на reactor/imgur и т.п.) показываем сразу картинкой, а не голой
// ссылкой. Работает на событии `load` каждой секции foliate-view.
//
// Два случая:
//   1) <a href="…jpg"> — под ссылкой показываем картинку (ссылку оставляем как
//      подпись/фолбэк, если картинка не загрузится);
//   2) голый URL картинки в тексте (напр. «Обложка: https://…jpeg») — делаем URL
//      кликабельным и вставляем картинку после него.

// Расширения, которые считаем картинкой (в pathname, до query).
const IMG_EXT = /\.(?:jpe?g|png|gif|webp|avif|bmp)$/i
// Голый URL картинки в тексте.
const URL_IMG_RE =
  /https?:\/\/[^\s<>"']+\.(?:jpe?g|png|gif|webp|avif|bmp)(?:\?[^\s<>"']*)?/gi

function isImageUrl(url) {
  try {
    return IMG_EXT.test(new URL(url, 'https://x/').pathname)
  } catch {
    return false
  }
}

function makeImg(doc, url) {
  const img = doc.createElement('img')
  img.src = url
  img.loading = 'lazy'
  img.alt = ''
  img.referrerPolicy = 'no-referrer' // многие хостинги (reactor) отдают только без реферера
  img.style.cssText =
    'max-width:100%;height:auto;display:block;margin:0.6em auto;border-radius:4px'
  return img
}

// <a href="…jpg"> → показать картинку сразу после ссылки.
function inlineAnchorImages(doc) {
  for (const a of doc.querySelectorAll('a[href]')) {
    if (a.dataset.imgInlined) continue
    const href = a.getAttribute('href')
    if (!href || !isImageUrl(href)) continue
    if (a.querySelector('img')) continue // ссылка уже обёрнута вокруг картинки
    a.dataset.imgInlined = '1'
    a.insertAdjacentElement('afterend', makeImg(doc, href))
  }
}

// Голый URL картинки в текстовом узле → кликабельная ссылка + картинка.
function inlineBareUrl(doc, node) {
  const text = node.nodeValue
  URL_IMG_RE.lastIndex = 0
  if (!URL_IMG_RE.test(text)) return
  URL_IMG_RE.lastIndex = 0

  const frag = doc.createDocumentFragment()
  let last = 0
  let m
  while ((m = URL_IMG_RE.exec(text))) {
    const before = text.slice(last, m.index)
    if (before) frag.appendChild(doc.createTextNode(before))
    const a = doc.createElement('a')
    a.href = m[0]
    a.textContent = m[0]
    a.target = '_blank'
    a.rel = 'noopener noreferrer'
    a.dataset.imgInlined = '1'
    frag.appendChild(a)
    frag.appendChild(makeImg(doc, m[0]))
    last = m.index + m[0].length
  }
  const rest = text.slice(last)
  if (rest) frag.appendChild(doc.createTextNode(rest))
  node.parentNode?.replaceChild(frag, node)
}

function inlineImages(doc) {
  inlineAnchorImages(doc)
  // Текстовые узлы с голым URL картинки (вне ссылок — ссылки уже обработаны выше).
  const walker = doc.createTreeWalker(
    doc.body || doc.documentElement,
    NodeFilter.SHOW_TEXT
  )
  const targets = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.nodeValue || n.nodeValue.indexOf('http') < 0) continue
    if (n.parentElement?.closest('a')) continue
    targets.push(n)
  }
  for (const n of targets) inlineBareUrl(doc, n)
}

// Обработчик события `load` foliate-view.
export function inlineImagesOnLoad(e) {
  const doc = e?.detail?.doc
  if (!doc) return
  try {
    inlineImages(doc)
  } catch {}
}
