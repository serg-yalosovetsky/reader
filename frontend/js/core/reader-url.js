// Формат относится к конкретной книге; посторонние параметры URL сохраняем.
export function parseReaderUrl(url) {
  const read = url.hash.match(/^#read\/(.+)$/)
  const page = url.hash.match(/^#book\/(.+)$/)
  const id = read || page ? decodeURIComponent((read || page)[1]) : url.searchParams.get('open')
  const value = url.searchParams.get('fmt')
  return id ? { id, fromHash: !!(read || page), page: !!page,
    fmt: ['pdf', 'epub'].includes(value) ? value : null } : null
}

export function readerUrl(url, id, fmt = null) {
  const next = new URL(url)
  next.searchParams.delete('open')
  next.searchParams.delete('original')
  if (['pdf', 'epub'].includes(fmt)) next.searchParams.set('fmt', fmt)
  else next.searchParams.delete('fmt')
  next.hash = `read/${encodeURIComponent(id)}`
  return next.pathname + next.search + next.hash
}
