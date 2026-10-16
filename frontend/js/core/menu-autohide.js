// Решения без DOM: листание не считается просьбой показать меню.
export const MENU_HIDE_MS = 5000
export function menuAutohideEnabled(format, prefs = {}) {
  return format?.toLowerCase() === 'pdf'
    ? (prefs.menuAutohidePdf ?? true) : (prefs.menuAutohideOther ?? false)
}
export function shouldRevealMenu({ type, key, center = false, mouse = false, y, height, panel = false }) {
  if (type === 'keydown') return key === 'Tab' || key === 'Escape'
  if (type === 'click') return center
  if (type === 'pointermove') return mouse && (panel || y <= 40 || y >= height - 40)
  return type === 'focusin' && panel
}
