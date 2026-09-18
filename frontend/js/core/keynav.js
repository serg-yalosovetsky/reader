// Какая навигация стоит за нажатой клавишей (serg/tasks#996).
//
// Отдельный модуль БЕЗ импортов — намеренно: navigation.js тянет десяток
// зависимостей (dom, api, prefs, state, tts, library…) и в node не грузится,
// а решение «куда ведёт клавиша» должно проверяться тестом, а не глазами.
// Тем же приёмом живёт core/fullscreen.js.
//
// Поручение владельца: End — в конец ГЛАВЫ, Ctrl+End — в конец КНИГИ; то же с
// Home. Раньше Home/End уводили сразу к краям КНИГИ, и добраться до конца
// длинной главы одной клавишей было нечем.

const PLAIN = {
  ArrowLeft: 'prev',
  ArrowRight: 'next',
  PageUp: 'prev',
  PageDown: 'next',
}

const STEP = new Set([' ', 'Spacebar', 'Enter'])

/**
 * @param {string} key — event.key
 * @param {{ctrl?: boolean, shift?: boolean}} mods
 * @returns {'prev'|'next'|'chapterStart'|'chapterEnd'|'bookStart'|'bookEnd'|null}
 *   null — клавиша не наша, событие достаётся браузеру.
 */
export function keyNavAction(key, { ctrl = false, shift = false } = {}) {
  if (key === 'Home') return ctrl ? 'bookStart' : 'chapterStart'
  if (key === 'End') return ctrl ? 'bookEnd' : 'chapterEnd'
  // Ctrl модифицирует ТОЛЬКО Home/End. Ctrl+PageDown у браузера — переключение
  // вкладок, Ctrl+стрелка — переход по словам: молчаливый угон чужого сочетания
  // человек заметит не сразу и списать его будет не на что.
  if (ctrl) return null
  if (STEP.has(key)) return shift ? 'prev' : 'next'
  return PLAIN[key] ?? null
}
