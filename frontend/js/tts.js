// Озвучивание книги (TTS) с подсветкой текущего слова.
import { $ } from './core/dom.js'
import { view, bookDoc } from './core/state.js'

// ===================== TTS =====================
export const ttsSt = { active: false, paused: false, chunks: [], idx: 0, rate: 1, voiceId: 'xenia', voiceLang: 'ru-RU', advance: false, currentEl: null, audio: null, rafId: null, wordIdx: 0, wordTimings: [], allVoices: [], _chunkBodyOffset: 0, prefetch: {} }

// Автодетект языка: Кириллица → русский голос, Latin → английский.
// Возвращает id голоса для этого текста (текущий выбор, если не сработала эвристика).
function pickVoiceForText(text) {
  const _cyr = (text.match(/[\u0400-\u04FF]/g) || []).length
  const _lat = (text.match(/[a-zA-Z]/g) || []).length
  let voiceId = ttsSt.voiceId
  if (_cyr > _lat * 0.5 + 2 && !ttsSt.voiceLang.startsWith('ru'))
    voiceId = (ttsSt.allVoices.find(v => v.lang.startsWith('ru')) || {}).id || voiceId
  else if (_lat > _cyr * 0.5 + 5 && !ttsSt.voiceLang.startsWith('en'))
    voiceId = (ttsSt.allVoices.find(v => v.lang.startsWith('en')) || {}).id || voiceId
  return voiceId
}

// Паттерн «визуального шума» — строки, которые TTS не должен произносить
const TTS_SKIP = /^[\s*\-~=_|•·×✦◦∗#—]{2,}$|^(\*\s+){2,}\*?$|^(-\s+){2,}-?$/

function ttsCleanLine(s) {
  if (TTS_SKIP.test(s)) return ''
  if (s.length <= 6 && /^[\d\s.,;:!?()/\\\[\]]+$/.test(s)) return ''
  s = s.replace(/https?:\/\/\S+/gi, '').trim()
  if (!s || s.length < 2) return ''
  return s
}

function ttsSplit(text) {
  const chunks = []
  for (const raw of text.split(/\n+/)) {
    const s = ttsCleanLine(raw.trim())
    if (!s || s.length < 2) continue
    if (s.length <= 3800) { chunks.push(s); continue }
    const sents = s.match(/[^.!?…]+[.!?…»]+\s*/g) || [s]
    for (const sent of sents) { const t = sent.trim(); if (t.length > 1) chunks.push(t) }
  }
  return chunks
}

// Заголовки, после которых начинается секция примечаний/сносок
const TTS_NOTES_RE = /^(примечани[яе]|сноск[аи]|footnotes?|notes?|переводчик|перевод\s*:)\s*:?\s*$/i

function ttsExtract() {
  if (!bookDoc) return []
  let blocks = [...bookDoc.querySelectorAll('p, h1, h2, h3, h4, h5, li, blockquote')]

  // Fallback: EPUB с текстом прямо в body (без <p>) — используем body.innerText
  const _iH = bookDoc.defaultView?.innerHeight || 0
  if (blocks.length < 5 && _iH > 2000) {
    try {
      const rawText = (bookDoc.body?.innerText || '').trim()
      if (rawText.length > 200) {
        const lines = rawText.split(/\n/).map(l => l.trim()).filter(l => l.length >= 5)
        const notesLine = lines.findIndex(l => TTS_NOTES_RE.test(l))
        const storyLines = notesLine > 0 ? lines.slice(0, notesLine) : lines
        if (storyLines.length > 0) {
          const pager = view?.renderer
          const pct = pager ? Math.max(0, pager.start / Math.max(_iH - (pager.size||800), 1)) : 0
          const startLine = Math.floor(pct * storyLines.length)
          return ttsSplit(storyLines.slice(Math.max(0, startLine - 1)).join('\n'))
        }
      }
    } catch {}
  }

  if (!blocks.length) return ttsSplit(bookDoc.body?.innerText || '')

  const notesIdx = blocks.findIndex(el => TTS_NOTES_RE.test(el.innerText?.trim() || ''))
  if (notesIdx > 0) blocks = blocks.slice(0, notesIdx)

  let startIdx = 0
  let startFound = false

  // 1. Старт с выделения
  try {
    const sel = bookDoc.getSelection()
    if (sel && !sel.isCollapsed) {
      let el = sel.anchorNode?.nodeType === Node.TEXT_NODE ? sel.anchorNode.parentElement : sel.anchorNode
      while (el && !['P','H1','H2','H3','H4','H5','LI','BLOCKQUOTE'].includes(el.tagName || '')) el = el.parentElement
      const i = el ? blocks.indexOf(el) : -1
      if (i >= 0) { startIdx = i; startFound = true }
    }
  } catch {}

  // 2. Определяем первый видимый блок
  if (!startFound) {
    try {
      const pager = view?.renderer
      const pgStart = pager?.start ?? 0
      const pgSize  = pager?.size  ?? 800
      if (pager?.scrolled) {
        // SCROLLED: iframe = вся глава (высота 30000-50000px), docScrollY=0
        // pager.start = #container.scrollTop = document-координата видимой области
        // elementFromPoint(x, y) принимает y в document-пространстве (не viewport!)
        // поэтому y = pager.start + смещение
        outer: for (let row = 0; row < 6; row++) {
          for (let col = 0; col < 5; col++) {
            const x = 20 + col * 80
            const y = pgStart + 15 + row * 30
            let node = bookDoc.elementFromPoint(x, y)
            while (node && node.tagName !== 'HTML' && node.tagName !== 'BODY') {
              const i = blocks.indexOf(node)
              if (i >= 0) { startIdx = i; startFound = true; break outer }
              node = node.parentElement
            }
          }
        }
        // Запасной вариант: первый блок, чья нижняя граница >= pgStart
        if (!startFound) {
          const i = blocks.findIndex(el => (el.offsetTop || 0) + (el.offsetHeight || 30) > pgStart)
          if (i >= 0) startIdx = i
        }
      } else {
        // PAGINATED: iframe горизонтально расширен, x = pager.start + offset
        outer: for (let row = 0; row < 5; row++) {
          for (let col = 0; col < 5; col++) {
            const x = pgStart + 20 + col * Math.round(pgSize * 0.18)
            const y = 15 + row * 30
            let node = bookDoc.elementFromPoint(x, y)
            while (node && node.tagName !== 'HTML' && node.tagName !== 'BODY') {
              const i = blocks.indexOf(node)
              if (i >= 0) { startIdx = i; break outer }
              node = node.parentElement
            }
          }
        }
      }
    } catch {}
  }

  // 3. Фильтр: только блоки видимой области
  let src
  try {
    const pager = view?.renderer
    if (pager) {
      const pgStart = pager.start ?? 0
      const pgEnd   = pgStart + (pager.size ?? 800)
      const pageBlocks = blocks.slice(startIdx).filter(el => {
        try {
          if (pager.scrolled) {
            // scrolled: offsetTop = document y = те же координаты что pager.start
            const top = el.offsetTop || 0
            return top + (el.offsetHeight || 30) > pgStart + 2 && top < pgEnd - 2
          } else {
            const r = el.getBoundingClientRect()
            return r.width > 0 && r.height > 0 && r.right > pgStart + 2 && r.left < pgEnd - 2
          }
        } catch { return false }
      })
      src = pageBlocks.length > 0 ? pageBlocks : blocks.slice(startIdx, startIdx + 8)
    }
  } catch {}
  if (!src) src = blocks.slice(startIdx)
  const extracted = src.map(el => el.innerText?.trim()).filter(Boolean).join('\n')

  // Если текст подозрительно короткий, а body содержит намного больше — fallback на body
  try {
    const bodyText = (bookDoc.body?.innerText || '').trim()
    if (extracted.length < 300 && bodyText.length > extracted.length + 500) {
      const lines = bodyText.split(/\n/).map(l => l.trim()).filter(l => l.length >= 5)
      const notesLine = lines.findIndex(l => TTS_NOTES_RE.test(l))
      const storyLines = notesLine > 0 ? lines.slice(0, notesLine) : lines
      if (storyLines.join('').length > extracted.length + 200) {
        const pager = view?.renderer
        const iH2 = bookDoc.defaultView?.innerHeight || 0
        const pct2 = (pager && iH2 > 0) ? Math.max(0, pager.start / Math.max(iH2 - (pager.size||800), 1)) : 0
        const sl = Math.floor(pct2 * storyLines.length)
        return ttsSplit(storyLines.slice(Math.max(0, sl - 1)).join('\n'))
      }
    }
  } catch {}

  return ttsSplit(extracted)
}

// Вспомогательные: подсветка абзаца и слова
function findTextOffset(el, searchText) {
  if (!el || !bookDoc || !searchText) return -1
  try {
    const walker = bookDoc.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    let pos = 0, node
    while ((node = walker.nextNode())) {
      const idx = node.nodeValue.indexOf(searchText)
      if (idx >= 0) return pos + idx
      pos += node.nodeValue.length
    }
  } catch {}
  return -1
}
function ttsClearHighlights() {
  if (!bookDoc) return
  try { bookDoc.querySelectorAll('.tts-reading').forEach(el => el.classList.remove('tts-reading')) } catch {}
  try { bookDoc.defaultView?.CSS?.highlights?.delete('tts-word') } catch {}
}
function findCharRange(root, start, len) {
  if (!bookDoc || !root) return null
  const walker = bookDoc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  let pos = 0, sNode, sOff, eNode, eOff, node
  while ((node = walker.nextNode())) {
    const nl = node.length
    if (!sNode && pos + nl > start) { sNode = node; sOff = start - pos }
    if (sNode && pos + nl >= start + len) { eNode = node; eOff = start + len - pos; break }
    pos += nl
  }
  if (!sNode) return null
  try {
    const r = bookDoc.createRange()
    r.setStart(sNode, Math.min(sOff, sNode.length))
    r.setEnd(eNode || sNode, Math.min(eOff ?? sOff + len, (eNode || sNode).length))
    return r
  } catch { return null }
}

function ttsPrecomputeWords(text, words) {
  let pos = 0
  const result = []
  for (const w of words || []) {
    if (!w.text) continue
    const idx = text.indexOf(w.text, pos)
    if (idx >= 0) { result.push({ t: w.t, charIndex: idx, charLength: w.text.length }); pos = idx + w.text.length }
  }
  return result
}

function ttsWordRaf() {
  if (!ttsSt.active || ttsSt.paused || !ttsSt.audio || !bookDoc) return
  const ms = ttsSt.audio.currentTime * 1000
  while (ttsSt.wordIdx < ttsSt.wordTimings.length && ttsSt.wordTimings[ttsSt.wordIdx].t <= ms) {
    const wt = ttsSt.wordTimings[ttsSt.wordIdx++]
    try {
      const range = findCharRange(bookDoc.body, ttsSt._chunkBodyOffset + wt.charIndex, wt.charLength)
      if (range) {
        const H = bookDoc.defaultView?.Highlight
        const hs = bookDoc.defaultView?.CSS?.highlights
        if (H && hs) hs.set('tts-word', new H(range))
        // Auto-scroll: keep highlighted word in view (scrolled mode only)
        try {
          const pager = view?.renderer
          if (pager?.scrolled) {
            const rect = range.getBoundingClientRect()
            const viewH = pager.size || bookDoc.defaultView?.innerHeight || 800
            if (rect.bottom > viewH * 0.82 || rect.top < 0) {
              const container = view.renderer.shadowRoot?.querySelector('#container')
              if (container) container.scrollBy({ top: rect.top - viewH * 0.25, behavior: 'smooth' })
            }
          }
        } catch {}
      }
    } catch {}
  }
  if (ttsSt.wordIdx < ttsSt.wordTimings.length) ttsSt.rafId = requestAnimationFrame(ttsWordRaf)
}

async function ttsSpeakChunk() {
  if (!ttsSt.active || ttsSt.idx >= ttsSt.chunks.length) {
    if (ttsSt.active) { ttsClearHighlights(); ttsSt.advance = true; view?.next() }
    return
  }
  const text = ttsSt.chunks[ttsSt.idx]
  ttsClearHighlights()
  ttsSt.wordIdx = 0; ttsSt.wordTimings = []

  // Позиция чанка в body для подсветки слов
  ttsSt._chunkBodyOffset = 0
  if (bookDoc?.body) {
    const s = text.trim().substring(0, 30)
    if (s) { const off = findTextOffset(bookDoc.body, s); if (off >= 0) ttsSt._chunkBodyOffset = off }
  }

  const total = ttsSt.chunks.length
  if (total) $('#tts-info').textContent = `${ttsSt.idx + 1} / ${total} (${Math.round((ttsSt.idx + 1) / total * 100)}%)`

  const voiceId = pickVoiceForText(text)

  const rateNum = Math.round((ttsSt.rate - 1) * 100)
  const rateStr = (rateNum >= 0 ? '+' : '') + rateNum + '%'

  // Prefetch: ждём если уже есть, иначе запрашиваем
  let audioUrl, wordTimingsReady
  if (ttsSt.prefetch[ttsSt.idx] instanceof Promise) {
    const cached = await ttsSt.prefetch[ttsSt.idx].catch(() => null)
    delete ttsSt.prefetch[ttsSt.idx]
    if (cached && ttsSt.active) { audioUrl = cached.audioUrl; wordTimingsReady = cached.wordTimings }
  }
  if (!audioUrl) {
    try {
      const resp = await fetch('/api/tts/synth', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: voiceId, rate: rateStr })
      })
      if (!resp.ok) throw new Error(await resp.text())
      const d = await resp.json()
      audioUrl = d.audio_url
      wordTimingsReady = ttsPrecomputeWords(text, d.words)
    } catch (e) {
      console.error('TTS synth:', e)
      if (ttsSt.active) { ttsSt.idx++; ttsSpeakChunk() }
      return
    }
  }
  if (!ttsSt.active) return

  ttsSt.wordTimings = wordTimingsReady || []
  ttsSt.wordIdx = 0

  // Prefetch следующего чанка пока играет текущий
  const _nextIdx = ttsSt.idx + 1
  if (ttsSt.active && _nextIdx < ttsSt.chunks.length && !ttsSt.prefetch[_nextIdx]) {
    const _nText = ttsSt.chunks[_nextIdx]
    const _nVoice = pickVoiceForText(_nText)
    ttsSt.prefetch[_nextIdx] = fetch('/api/tts/synth', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: _nText, voice: _nVoice, rate: rateStr })
    }).then(r => r.ok ? r.json() : null)
      .then(d => d ? { audioUrl: d.audio_url, wordTimings: ttsPrecomputeWords(_nText, d.words) } : null)
      .catch(() => null)
  }

  const audio = new Audio(audioUrl)
  ttsSt.audio = audio
  audio.addEventListener('ended', () => {
    if (ttsSt.audio !== audio) return
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
    if (ttsSt.active && !ttsSt.paused) { ttsSt.idx++; ttsSpeakChunk() }
  })
  audio.addEventListener('error', () => {
    if (ttsSt.audio !== audio) return
    if (ttsSt.active) { ttsSt.idx++; ttsSpeakChunk() }
  })
  try { await audio.play() } catch {}
  ttsSt.rafId = requestAnimationFrame(ttsWordRaf)
}

export function ttsReadPage() {
  ttsSt.chunks = ttsExtract()
  ttsSt.idx = 0
  ttsSpeakChunk()
}

function ttsStart() {
  if (!view) return
  const _oldAudio = ttsSt.audio; ttsSt.audio = null
  if (_oldAudio) { _oldAudio.pause(); _oldAudio.src = '' }
  if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
  ttsSt.active = true; ttsSt.paused = false; ttsSt.advance = false
  // Возобновить с сохранённой позиции или начать с нуля
  if (ttsSt.chunks.length > 0 && ttsSt.idx < ttsSt.chunks.length) {
    ttsSpeakChunk()
  } else {
    ttsReadPage()
  }
  ttsUpdateUI()
}

export function ttsStop() {
  ttsSt.active = false; ttsSt.paused = false; ttsSt.advance = false; ttsSt.prefetch = {}
  const _oldAudio = ttsSt.audio; ttsSt.audio = null
  if (_oldAudio) { _oldAudio.pause(); _oldAudio.src = '' }
  if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
  ttsClearHighlights()
  ttsUpdateUI()
}

function ttsPauseResume() {
  if (!ttsSt.active) { ttsStart(); return }
  if (!ttsSt.paused) {
    ttsSt.paused = true
    if (ttsSt.audio) ttsSt.audio.pause()
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
  } else {
    ttsSt.paused = false
    if (ttsSt.audio) { ttsSt.audio.play().catch(() => {}); ttsSt.rafId = requestAnimationFrame(ttsWordRaf) }
    else ttsSpeakChunk()
  }
  ttsUpdateUI()
}

function ttsUpdateUI() {
  const bar = $('#tts-bar'), btn = $('#tts-btn')
  if (!bar || !btn) return
  if (ttsSt.active) {
    bar.hidden = false
    $('#tts-play').textContent = ttsSt.paused ? '▶' : '⏸'
    btn.classList.add('tts-active')
  } else {
    bar.hidden = true
    $('#tts-info').textContent = ''
    btn.classList.remove('tts-active')
  }
}

async function ttsLoadVoices() {
  const sel = $('#tts-voice')
  if (!sel) return
  try {
    const data = await fetch('/api/tts/voices').then(r => r.json())
    const voices = data.voices || []
    ttsSt.allVoices = voices
    sel.innerHTML = ''
    const groups = {}
    for (const v of voices) {
      const grp = v.lang.startsWith('ru') ? 'Русский' : v.lang.startsWith('uk') ? 'Українська' : 'English'
      if (!groups[grp]) groups[grp] = []
      groups[grp].push(v)
    }
    for (const [label, list] of Object.entries(groups)) {
      if (!list.length) continue
      const g = document.createElement('optgroup')
      g.label = label
      for (const v of list) {
        const o = document.createElement('option')
        o.value = v.id; o.textContent = v.name
        if (v.id === ttsSt.voiceId) o.selected = true
        g.append(o)
      }
      sel.append(g)
    }
    if (!sel.value && voices.length) {
      const first = voices[0]
      sel.value = first.id; ttsSt.voiceId = first.id; ttsSt.voiceLang = first.lang
    }
  } catch (e) { console.error('ttsLoadVoices:', e) }
}
ttsLoadVoices()

$('#tts-btn').addEventListener('click', () => ttsSt.active ? ttsPauseResume() : ttsStart())
$('#tts-stop').addEventListener('click', ttsStop)
$('#tts-play').addEventListener('click', ttsPauseResume)
$('#tts-rate').addEventListener('change', (e) => {
  ttsSt.rate = parseFloat(e.target.value)
  if (ttsSt.active) {
    ttsSt.prefetch = {}
    const _ra = ttsSt.audio; ttsSt.audio = null
    if (_ra) { _ra.pause(); _ra.src = '' }
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
    ttsSpeakChunk()
  }
})
$('#tts-voice').addEventListener('change', (e) => {
  const v = ttsSt.allVoices.find(v => v.id === e.target.value)
  ttsSt.voiceId = v?.id || e.target.value; ttsSt.voiceLang = v?.lang || ''
  if (ttsSt.active) {
    ttsSt.prefetch = {}
    const _va = ttsSt.audio; ttsSt.audio = null
    if (_va) { _va.pause(); _va.src = '' }
    if (ttsSt.rafId) { cancelAnimationFrame(ttsSt.rafId); ttsSt.rafId = null }
    ttsSpeakChunk()
  }
})
