// Встроенный переводчик: показать видимый текст книги по-русски и вернуть
// оригинал повторным нажатием.
//
// Переводится ВИДИМЫЙ экран и один следующий, а не глава целиком: глава на 40
// тысяч знаков — это десятки секунд ожидания и мусорный расход на текст, до
// которого читатель может не дойти.
//
// Видимый экран и запас идут РАЗНЫМИ запросами, причём запас — только после
// того, как видимое уже подменено на экране: экран из 12 абзацев на живом
// gateway занимает ~11 с, и удваивать это ожидание ради текста, который ещё не
// на виду, нельзя.
//
// Оригинал не выбрасывается: он лежит рядом (WeakMap на узел), поэтому возврат
// мгновенный и не требует перезагрузки главы.
//
// Перевод тоже не выбрасывается — он остаётся в кэше вкладки (Map по тексту
// абзаца) и применяется БЕЗ СЕТИ. Серверный кэш в БД от повторного запроса не
// спасает: попадание в него всё равно стоит round-trip и полосы загрузки, а
// «показать оригинал → перевести» — самое частое действие при чтении.
import { $, toast } from './core/dom.js'
import { api } from './core/api.js'
import { logErr } from './core/log.js'
import { view, bookDoc } from './core/state.js'
import { setMoreActive, showTopLoading } from './chrome.js'

const BLOCKS = 'p, h1, h2, h3, h4, h5, h6, li, blockquote, dd, figcaption'
// Сколько экранов вперёд готовим: 1 = видимое + следующий столько же блоков.
const LOOKAHEAD = 1
// Минимальная длина блока: «— Да.» и номера страниц переводить незачем.
const MIN_LEN = 3

let on = false
let busy = false
let seq = 0
// Узел -> клон его исходного содержимого (DocumentFragment). Храним узлы, а не
// строку HTML: восстановление тогда не проходит через парсер разметки вовсе.
// WeakMap — главы выгружаются, ссылки на узлы мёртвых документов держать нельзя.
const originals = new WeakMap()
// Пометка «этот узел уже переведён» — чтобы листание не переводило заново.
const done = new WeakSet()
// Кэш вкладки: текст абзаца -> перевод. Ключ по СОДЕРЖИМОМУ (как и на сервере),
// поэтому переживает перезагрузку главы, возврат назад и повторное открытие
// книги — то есть ровно те случаи, ради которых кэш и нужен.
const cache = new Map()
// Абзацы, которые переводить не надо (уже на целевом языке): их повторный
// запрос стоил бы столько же, сколько настоящий перевод.
const skipCache = new Set()
// Потолок: книга на сотни тысяч абзацев не должна съесть память вкладки.
// Вытеснение FIFO — Map хранит порядок вставки, старейший ключ идёт первым.
const TR_CACHE_MAX = 4000

function cachePut(src, text) {
  // src === text означает, что ключом стал уже ПЕРЕВЕДЁННЫЙ текст: такой
  // записи в кэше быть не должно, она сделала бы перевод неотличимым от
  // оригинала. Сейчас это недостижимо (подстановка всегда идёт по оригиналу),
  // но защита дешевле, чем разбирательство, если порядок вызовов изменится.
  if (!src || src === text) return
  if (cache.size >= TR_CACHE_MAX) cache.delete(cache.keys().next().value)
  cache.set(src, text)
}

// Подменить всё, что уже известно этой вкладке, СИНХРОННО. Возвращает блоки,
// которых в кэше нет — только они и уходят в запрос.
function applyCached(els) {
  const miss = []
  for (const el of els) {
    const src = (el.textContent || '').trim()
    if (skipCache.has(src)) { done.add(el); continue }
    const hit = cache.get(src)
    if (hit) applyText(el, hit)
    else miss.push(el)
  }
  return miss
}

export function isTranslateOn() { return on }

// Пересечение с окном документа книги: в «Ленте» содержимое едет по Y, в
// «Страницах» foliate сдвигает колонки по X — проверка по обеим осям покрывает
// оба режима без ветвления по prefs.flow.
function visibleBlocks(doc) {
  const win = doc.defaultView
  if (!win) return { visible: [], all: [] }
  const W = win.innerWidth || 0, H = win.innerHeight || 0
  const all = [...doc.querySelectorAll(BLOCKS)]
  const visible = []
  for (const el of all) {
    const r = el.getBoundingClientRect()
    if (r.width === 0 && r.height === 0) continue
    if (r.bottom > 0 && r.top < H && r.right > 0 && r.left < W) visible.push(el)
  }
  return { visible, all }
}

function worthTranslating(el) {
  if (done.has(el)) return false
  return ((el.textContent || '').trim()).length >= MIN_LEN
}

// Видимое сейчас.
function currentBlocks(doc) {
  return visibleBlocks(doc).visible.filter(worthTranslating)
}

// Запас: столько же блоков сразу за последним видимым.
function aheadBlocks(doc) {
  const { visible, all } = visibleBlocks(doc)
  if (!visible.length) return []
  const last = all.indexOf(visible[visible.length - 1])
  if (last < 0) return []
  const extra = Math.max(1, visible.length * LOOKAHEAD)
  return all.slice(last + 1, last + 1 + extra).filter(worthTranslating)
}

// Перевод — плоский текст, поэтому курсив и ссылки внутри абзаца в переведённом
// виде не сохраняются (структура фраз при переводе всё равно не совпадает).
// Оригинальная разметка возвращается целиком при выключении.
function applyText(el, text) {
  cachePut((el.textContent || '').trim(), text)
  if (!originals.has(el)) {
    const frag = el.ownerDocument.createDocumentFragment()
    for (const n of el.childNodes) frag.append(n.cloneNode(true))
    originals.set(el, frag)
  }
  el.textContent = text
  el.dataset.trOn = '1'
  done.add(el)
}

function restoreDoc(doc) {
  if (!doc) return
  for (const el of doc.querySelectorAll('[data-tr-on]')) {
    const frag = originals.get(el)
    if (frag) {
      el.textContent = ''
      // Клонируем повторно: сам фрагмент при вставке опустеет, а глава может
      // быть переведена и возвращена ещё не раз.
      el.append(frag.cloneNode(true))
    }
    delete el.dataset.trOn
    done.delete(el)
  }
}

// quiet=true — фоновый запас: без полосы загрузки и без жалоб в тостах, читатель
// про этот запрос не знает и ждать его не должен.
async function runTranslate(doc, els, quiet) {
  if (!doc || !els.length) return 0
  const mySeq = ++seq
  busy = true
  if (!quiet) showTopLoading(true)
  try {
    const items = els.map((el, i) => ({ id: String(i), text: (el.textContent || '').trim() }))
    const res = await api.post('/api/translate', { items, target: 'ru' })
    // Пока ждали ответ, читатель мог выключить перевод или сменить главу —
    // подменять текст задним числом нельзя.
    if (!on || mySeq !== seq || doc !== bookDoc) return 0
    let changed = 0
    for (const it of res.items || []) {
      const el = els[Number(it.id)]
      if (!el) continue
      if (it.changed && it.text) { applyText(el, it.text); changed++ }
      else {
        // Уже на целевом языке или батч не удался — второй раз в этом проходе
        // не просим. Но ЗАПОМИНАЕМ только первое: признак берём по самому
        // абзацу (it.skipped), а не по ответу целиком. Один упавший абзац из
        // двенадцати не должен лишать остальные одиннадцать кэша — на книге,
        // где русский и английский вперемешку, это самый частый случай.
        done.add(el)
        if (it.skipped) skipCache.add((el.textContent || '').trim())
      }
    }
    if (!quiet && !changed && !(res.translated || res.cached)) {
      // Ничего не поменялось и переводить было нечего: текст уже по-русски.
      toast('Текст уже на русском', 'info')
      setOn(false)
      return 0
    }
    if (!quiet && res.failed) toast(`Часть абзацев не перевелась (${res.failed})`, 'err')
    return changed
  } catch (e) {
    logErr('translate', e)
    if (!quiet) {
      toast(e?.message?.includes('503') ? 'Перевод не настроен на сервере'
        : 'Не удалось перевести (сеть или движок)', 'err')
      setOn(false)
    }
    return 0
  } finally {
    busy = false
    if (!quiet) showTopLoading(false)
  }
}

// Сначала — всё, что вкладка уже знает (синхронно, без сети и без полосы
// загрузки), и только потом запрос за остатком: видимый экран сразу, запас
// следом и только если режим ещё включён.
async function translateCurrent(doc) {
  if (!doc) return
  const miss = applyCached(currentBlocks(doc))
  if (miss.length && !busy) await runTranslate(doc, miss, false)
  if (!on || doc !== bookDoc) return
  const aheadMiss = applyCached(aheadBlocks(doc))
  if (aheadMiss.length && !busy) runTranslate(doc, aheadMiss, true)
}

function setOn(next) {
  on = next
  const btn = $('#translate-btn')
  if (btn) {
    btn.setAttribute('aria-pressed', on ? 'true' : 'false')
    const st = $('#translate-state')
    if (st) st.textContent = on ? 'вкл' : ''
    const lbl = btn.querySelector('.more-label')
    if (lbl) lbl.textContent = on ? 'Показать оригинал' : 'Перевести на русский'
  }
  // Режим включён — видно снаружи по подсветке кнопки ⋮ (меню закрыто).
  setMoreActive(on ? 'translate' : '')
}

export function toggleTranslate() {
  if (on) {
    setOn(false)
    seq++            // отменить применение ответа, если он ещё в пути
    restoreDoc(bookDoc)
    return
  }
  setOn(true)
  translateCurrent(bookDoc)
}

// Новая глава загрузилась: если режим включён, переводим её первый экран.
export function onTranslateDocLoaded(doc) {
  if (on) translateCurrent(doc)
}

// Листание: подтянуть абзацы, доехавшие в поле зрения. Дебаунс — relocate
// прилетает на каждый кадр прокрутки в «Ленте».
let relocTimer = null
export function onTranslateRelocate() {
  if (!on) return
  clearTimeout(relocTimer)
  relocTimer = setTimeout(() => translateCurrent(bookDoc), 400)
}

// Смена книги: состояние сбрасываем, чтобы новая книга открылась в оригинале.
export function resetTranslate() {
  if (on) setOn(false)
  seq++
}

export function initTranslate() {
  $('#translate-btn')?.addEventListener('click', () => {
    if (!view) return
    toggleTranslate()
  })
  setOn(false)
}
