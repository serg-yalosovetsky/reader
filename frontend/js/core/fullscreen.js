// Полноэкранный режим читалки и системное «назад» (serg/tasks#902, #903).
//
// На Android первое «назад» в полноэкранном режиме браузер тратит на выход из него:
// popstate не приходит, и до библиотеки приходилось нажимать дважды. Поэтому выход
// из полноэкранного режима НЕ нашей кнопкой на сенсорном экране считаем «назад».
// Esc на десктопе — просто выход: закрывать книгу по Esc никто не просил.
// Модуль без импортов — его логика проверяется тестом tests/js/test_fullscreen_back.mjs.

let buttonExit = false

// Выход инициирован нами (кнопка ✖ или закрытие читалки) — это не «назад».
export function markButtonExit() { buttonExit = true }

export function takeButtonExit() {
  const v = buttonExit
  buttonExit = false
  return v
}

export function exitMeansBack({ readerOpen, byButton, coarse }) {
  return Boolean(readerOpen && !byButton && coarse)
}

export function isFullscreen(d = document) {
  return Boolean(d.fullscreenElement || d.webkitFullscreenElement)
}

export function exitFullscreen(d = document) {
  if (!isFullscreen(d)) return
  markButtonExit()
  const fn = d.exitFullscreen || d.webkitExitFullscreen
  try {
    const p = fn?.call(d)
    // Отказ промиса значит «документ уже не полноэкранный» — ровно то, чего добивались.
    p?.catch?.(() => {})
  } catch {
    // Старые WebKit бросают синхронно в том же случае; состояние и так нужное.
  }
}
