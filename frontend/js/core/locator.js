// Единая точка знания о форматах locator.
// Веб пишет foliate-CFI ("epubcfi(...)"), Android — Readium-Locator JSON.
// Общий кросс-девайс якорь для перехода — доля прочитанного (ratio).

// Валидный web-CFI? Только такие можно рисовать/удалять как оверлей-аннотацию;
// чужой (Android) locator оверлеем не отображается.
export function isWebCfi(locator) {
  return typeof locator === 'string' && locator.startsWith('epubcfi(')
}

// Перейти к сохранённой позиции: пробуем точный locator, при неудаче или его
// отсутствии (напр. кросс-девайс формат) откатываемся на goToFraction(ratio).
export async function goToLocator(view, locator, ratio = 0) {
  try {
    if (locator) { await view.goTo(locator); return }
  } catch {}
  try { await view.goToFraction(ratio || 0) } catch {}
}
