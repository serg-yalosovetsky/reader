// Отладочные замеры окна на экране (serg/tasks#902): после выхода из полноэкранного
// режима на телефоне верх читалки срезан, а под шкалой пустой запас. Автоматизация
// полноэкранный режим включить не может, поэтому цифры снимаются с живого телефона
// скриншотом. Включается ?vpdebug=1 (запоминается), выключается ?vpdebug=0.
// Без флага модуль ничего не делает.
const KEY = 'reader:vpdebug'

function enabled() {
  const p = new URLSearchParams(location.search).get('vpdebug')
  try {
    if (p === '1') localStorage.setItem(KEY, '1')
    if (p === '0') localStorage.removeItem(KEY)
    return localStorage.getItem(KEY) === '1'
  } catch {
    // Хранилище запрещено: замеры работают, только пока параметр стоит в адресе.
    return p === '1'
  }
}

function probe(css) {
  const el = document.createElement('div')
  el.style.cssText = 'position:fixed;left:0;top:0;width:0;visibility:hidden;pointer-events:none;' + css
  document.body.appendChild(el)
  return el
}

function start() {
  const box = document.createElement('pre')
  box.id = 'vpdebug'
  box.style.cssText = 'position:fixed;left:50%;top:40%;transform:translateX(-50%);z-index:10000;'
    + 'margin:0;padding:6px 8px;font:11px/1.35 monospace;background:rgba(0,0,0,.8);color:#7f7;'
    + 'border-radius:6px;pointer-events:none;white-space:pre'
  document.body.appendChild(box)
  const insetB = probe('height:0;padding-bottom:env(safe-area-inset-bottom,0px)')
  const insetT = probe('height:0;padding-top:env(safe-area-inset-top,0px)')
  const svh = probe('height:100svh')
  const dvh = probe('height:100dvh')
  const lvh = probe('height:100lvh')
  const px = (n) => Math.round(n || 0)
  const vv = window.visualViewport

  function update() {
    const de = document.documentElement
    const lines = [
      `inner ${innerWidth}x${innerHeight}  vv ${px(vv?.height)} off ${px(vv?.offsetTop)}`,
      `client ${de.clientHeight}  scrollY ${px(scrollY)}  html ${de.scrollTop} body ${document.body.scrollTop}`,
      `svh ${svh.offsetHeight}  dvh ${dvh.offsetHeight}  lvh ${lvh.offsetHeight}`,
      `inset bottom ${insetB.offsetHeight}  top ${insetT.offsetHeight}`,
      `fullscreen ${Boolean(document.fullscreenElement || document.webkitFullscreenElement)}`,
    ]
    const r = document.getElementById('reader')
    if (r && !r.hidden) {
      const rr = r.getBoundingClientRect()
      lines.push(`reader top ${px(rr.top)} h ${px(rr.height)}`)
      const t = document.getElementById('reader-top')
      if (t) lines.push(`top-bar top ${px(t.getBoundingClientRect().top)} h ${t.offsetHeight}`)
      const b = document.getElementById('reader-bottom')
      if (b) {
        const br = b.getBoundingClientRect()
        lines.push(`bottom-bar top ${px(br.top)} h ${px(br.height)} pad-b ${getComputedStyle(b).paddingBottom}`)
      }
    }
    box.textContent = lines.join('\n')
  }

  update()
  for (const ev of ['resize', 'scroll', 'orientationchange']) window.addEventListener(ev, update, { passive: true })
  document.addEventListener('fullscreenchange', update)
  vv?.addEventListener('resize', update)
  // Раз в секунду — чтобы поймать состояние после открытия книги без событий окна.
  setInterval(update, 1000)
}

if (typeof document !== 'undefined' && enabled()) {
  if (document.body) start()
  else document.addEventListener('DOMContentLoaded', start)
}
