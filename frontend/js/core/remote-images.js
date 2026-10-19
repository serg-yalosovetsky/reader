// Убираем сетевые картинки до iframe.load, сохраняя размеры и локальные ресурсы.
const remote = /^https?:\/\//i
export function deferRemoteImages(html) {
  return html.replace(/<(?:img|image)\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi, tag => {
    const saved = []
    const result = tag.replace(/\s+([^\s=/>]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, (attr, name, quoted) => {
      if (!['src', 'href', 'xlink:href', 'srcset'].includes(name.toLowerCase())) return attr
      const value = /^["']/.test(quoted) ? quoted.slice(1, -1) : quoted
      if (!(name.toLowerCase() === 'srcset' ? /(?:^|[,\s])https?:\/\//i.test(value) : remote.test(value))) return attr
      saved.push(` data-remote-${name.toLowerCase().replace(':', '-')}=${/^["']/.test(quoted) ? quoted : '"' + quoted + '"'}`)
      return ''
    })
    return result.replace(/(\/?>)$/, saved.join('') + '$1')
  })
}

export function restoreRemoteImages(doc) {
  for (const el of doc.querySelectorAll('img, image')) {
    const attrs = ['src', 'href', 'xlink:href', 'srcset']
    if (!attrs.some(name => el.hasAttribute('data-remote-' + name.replace(':', '-')))) continue
    el.setAttribute('loading', 'lazy')
    for (const name of attrs) {
      const key = 'data-remote-' + name.replace(':', '-')
      if (!el.hasAttribute(key)) continue
      el.setAttribute(name, el.getAttribute(key))
      el.removeAttribute(key)
    }
  }
}

export function protectRemoteImages(book) {
  if (book.rendition?.layout === 'pre-paginated' || book.rendition?.layout === 'fixed') return false
  book.transformTarget?.addEventListener('data', ({ detail }) => {
    if (['application/xhtml+xml', 'text/html'].includes(detail.type))
      detail.data = Promise.resolve(detail.data).then(deferRemoteImages)
  })
  return !!book.transformTarget
}
