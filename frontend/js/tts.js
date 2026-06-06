// Озвучивание книги (TTS) с подсветкой текущего слова.
//
// Идея: для текущей секции foliate собираем слова с их DOM-Range, плоский текст
// шлём на /api/tts/synth (edge-tts) и получаем mp3 + пословные тайминги
// [{t,d,text}] (мс). Играем <audio>, по audio.currentTime находим активное слово,
// подсвечиваем его соответствующий Range через CSS Custom Highlight API и держим
// в зоне видимости (foliate scrollToAnchor — листает страницы/прокручивает ленту).
// Скорость — через audio.playbackRate (тайминги в media-времени не плывут, ре-синтез
// не нужен). Смена голоса требует ре-синтеза. По концу секции — переход к следующей.

const normalize = (s) => (s || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '')

// Собрать слова текущего документа книги: для каждого — Range + текст, по порядку.
function extractTokens(doc) {
  const root = doc.body || doc.documentElement
  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT
      const p = n.parentElement
      if (p && /^(script|style|svg|head|noscript)$/i.test(p.tagName)) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    },
  })
  const tokens = []
  const parts = []
  let node
  while ((node = walker.nextNode())) {
    const re = /\S+/g
    let m
    while ((m = re.exec(node.nodeValue))) {
      const range = doc.createRange()
      range.setStart(node, m.index)
      range.setEnd(node, m.index + m[0].length)
      tokens.push({ range, text: m[0] })
      parts.push(m[0])
    }
  }
  return { text: parts.join(' '), tokens }
}

// Сопоставить произнесённые edge-tts слова (по порядку) с токенами DOM.
// Возвращает массив Range|null длиной words.length. Допускаем рассинхрон
// (пунктуация, числа словами): ищем вперёд в окне, не найдя — оставляем null.
function buildWordRanges(tokens, words) {
  const ranges = new Array(words.length).fill(null)
  let p = 0
  const WINDOW = 12
  for (let i = 0; i < words.length; i++) {
    const target = normalize(words[i].text)
    if (!target) continue
    for (let j = p; j < Math.min(tokens.length, p + WINDOW); j++) {
      const nt = normalize(tokens[j].text)
      if (!nt) continue
      if (nt === target || nt.startsWith(target) || target.startsWith(nt)) {
        ranges[i] = tokens[j].range
        p = j + 1
        break
      }
    }
  }
  return ranges
}

export class TTSController {
  constructor(view, ui) {
    this.view = view
    this.ui = ui // { setStatus, setPlaying, show, hide }
    this.audio = new Audio()
    this.audio.preload = 'auto'
    this.voice = 'svetlana'
    this.rate = 1
    this.words = []
    this.ranges = []
    this.cur = -1
    this.playing = false
    this.doc = null
    this.hl = null
    this.raf = null
    this.audio.addEventListener('ended', () => this._onEnded())
    this.audio.addEventListener('error', () => this.ui.setStatus('Ошибка аудио'))
  }

  _contents() { return (this.view?.renderer?.getContents?.() || [])[0] || null }

  _ensureHighlightStyle(doc) {
    if (doc.getElementById('tts-hl-style')) return
    const st = doc.createElement('style')
    st.id = 'tts-hl-style'
    st.textContent = '::highlight(tts-word){background:#e8853a;color:#fff;border-radius:3px}'
    ;(doc.head || doc.documentElement).append(st)
  }

  _clearHighlight() {
    try { this.doc?.defaultView?.CSS?.highlights?.delete('tts-word') } catch {}
    this.hl = null
    this.cur = -1
  }

  // Синтез текущей секции. false — секция без текста (надо листать дальше).
  async _loadCurrentSection() {
    const c = this._contents()
    if (!c) throw new Error('нет текущей секции')
    this.doc = c.doc
    const { text, tokens } = extractTokens(this.doc)
    if (!text.trim()) return false
    this._ensureHighlightStyle(this.doc)
    const r = await fetch('/api/tts/synth', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice: this.voice }),
    })
    if (!r.ok) throw new Error('синтез не удался (' + r.status + ')')
    const data = await r.json()
    this.words = data.words || []
    this.ranges = buildWordRanges(tokens, this.words)
    this.audio.src = data.audio_url
    this.audio.playbackRate = this.rate
    this.cur = -1
    return true
  }

  async start() {
    try {
      this.ui.show()
      this.ui.setStatus('Готовлю озвучивание…')
      let ok = await this._loadCurrentSection()
      // Пропускаем пустые секции (титулы/картинки) до текстовой.
      let guard = 0
      while (!ok && guard++ < 20) {
        if (!(await this._nextSection())) { this.stop(); return }
        ok = await this._loadCurrentSection()
      }
      if (!ok) { this.stop(); return }
      await this.audio.play()
      this.playing = true
      this.ui.setPlaying(true)
      this.ui.setStatus('Озвучивание…')
      this._tick()
    } catch (e) {
      this.ui.setStatus('Ошибка: ' + (e?.message || e))
    }
  }

  toggle() { this.playing ? this.pause() : this.resume() }

  pause() {
    this.audio.pause(); this.playing = false; this.ui.setPlaying(false)
    cancelAnimationFrame(this.raf)
  }

  resume() {
    if (!this.audio.src) return this.start()
    this.audio.play(); this.playing = true; this.ui.setPlaying(true); this._tick()
  }

  stop() {
    this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load?.()
    this.playing = false; this.ui.setPlaying(false)
    cancelAnimationFrame(this.raf)
    this._clearHighlight()
    this.ui.hide()
  }

  setRate(r) { this.rate = r; this.audio.playbackRate = r }

  async setVoice(v) {
    this.voice = v
    if (!this.audio.src) return
    const wasPlaying = this.playing
    const pos = this.audio.currentTime
    this.audio.pause()
    try {
      await this._loadCurrentSection()
      this.audio.currentTime = Math.min(pos, this.audio.duration || pos)
      if (wasPlaying) { await this.audio.play(); this._tick() }
    } catch (e) { this.ui.setStatus('Ошибка смены голоса: ' + (e?.message || e)) }
  }

  // Перейти к следующей линейной секции. false — секций больше нет.
  async _nextSection() {
    const c = this._contents()
    const sections = this.view?.book?.sections || []
    let idx = (c?.index ?? -1) + 1
    while (idx < sections.length && sections[idx]?.linear === 'no') idx++
    if (idx >= sections.length) return false
    await this.view.renderer.goTo({ index: idx, anchor: 0 })
    return true
  }

  async _onEnded() {
    if (!this.playing) return
    this.ui.setStatus('Следующая секция…')
    let ok = false, guard = 0
    while (!ok && guard++ < 20) {
      if (!(await this._nextSection())) { this.ui.setStatus('Книга дочитана'); this.stop(); return }
      ok = await this._loadCurrentSection()
    }
    if (!ok) { this.stop(); return }
    await this.audio.play()
    this.ui.setStatus('Озвучивание…')
    this._tick()
  }

  _findWord(ms) {
    const w = this.words
    let lo = 0, hi = w.length - 1, res = -1
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if (w[mid].t <= ms) { res = mid; lo = mid + 1 } else hi = mid - 1
    }
    return res
  }

  _tick() {
    if (!this.playing) return
    const ms = this.audio.currentTime * 1000
    const i = this._findWord(ms)
    if (i !== this.cur) { this.cur = i; this._highlight(i) }
    this.raf = requestAnimationFrame(() => this._tick())
  }

  _highlight(i) {
    const range = this.ranges[i]
    if (!range) return
    const win = this.doc?.defaultView
    if (win?.CSS?.highlights && win.Highlight) {
      if (!this.hl) { this.hl = new win.Highlight(); win.CSS.highlights.set('tts-word', this.hl) }
      this.hl.clear(); this.hl.add(range)
    }
    // Держим слово в зоне видимости (листание страниц / прокрутка ленты).
    if (!this._inView(range)) {
      try { this.view.renderer.scrollToAnchor(range) } catch {}
    }
  }

  _inView(range) {
    try {
      const r = range.getBoundingClientRect()
      const win = this.doc.defaultView
      const W = win.innerWidth || 0, H = win.innerHeight || 0
      if (!r.width && !r.height) return true
      return r.bottom > 0 && r.top < H && r.right > 0 && r.left < W
    } catch { return true }
  }
}
