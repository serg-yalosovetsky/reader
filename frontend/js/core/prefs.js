// Настройки вида (localStorage): тема/шрифт/поля/режим/колонки.
const PREFS_KEY = 'reader.prefs'
export const prefs = Object.assign(
  { theme: 'day', fontScale: 1, marginLevel: 1, fontFamily: 'merriweather', flow: 'paginated', columns: 1 },
  JSON.parse(localStorage.getItem(PREFS_KEY) || '{}'),
)
if (prefs.fontFamily === 'serif') prefs.fontFamily = 'merriweather'
if (prefs.fontFamily === 'sans')  prefs.fontFamily = 'open-sans'
export const savePrefs = () => localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))
export const MARGIN_INLINE = { 0: 760, 1: 620, 2: 480 } // уровень полей -> max-inline-size (меньше = шире поля)
export const MARGIN_NAME = { 0: 'узк.', 1: 'сред.', 2: 'шир.' }
export const FONT_STACKS = {
  'merriweather': '"Merriweather", Georgia, serif',
  'lora':         '"Lora", Georgia, serif',
  'pt-serif':     '"PT Serif", Georgia, serif',
  'georgia':      'Georgia, "Times New Roman", serif',
  'open-sans':    '"Open Sans", system-ui, sans-serif',
  'nunito':       '"Nunito", system-ui, sans-serif',
  'pt-sans':      '"PT Sans", system-ui, sans-serif',
}
export const GFONTS = 'https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,700;1,400;1,700&family=Merriweather:ital,wght@0,400;0,700;1,400;1,700&family=Nunito:ital,wght@0,400;0,700;1,400;1,700&family=Open+Sans:ital,wght@0,400;0,700;1,400;1,700&family=PT+Serif:ital,wght@0,400;0,700;1,400;1,700&family=PT+Sans:ital,wght@0,400;0,700;1,400;1,700&display=swap'

document.documentElement.dataset.theme = prefs.theme
