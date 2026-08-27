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
// Имя семейства у Google Fonts для каждого нашего ключа. Georgia здесь нет
// намеренно: она системная, её грузить неоткуда и незачем.
export const GFONT_NAMES = {
  'merriweather': 'Merriweather',
  'lora':         'Lora',
  'pt-serif':     'PT Serif',
  'open-sans':    'Open Sans',
  'nunito':       'Nunito',
  'pt-sans':      'PT Sans',
}

// URL шрифта ТОЛЬКО для выбранного семейства.
// Раньше здесь была одна константа на все шесть семейств по четыре начертания —
// 24 файла со стороннего хоста в каждом документе книги. Читатель видит одно
// семейство, а ждал загрузки всех: рендер книги гейтится на document.fonts.ready.
export function gfontsFor(family) {
  const name = GFONT_NAMES[family]
  if (!name) return ''   // системный шрифт — сеть не нужна вовсе
  const fam = name.replace(/ /g, '+')
  return `https://fonts.googleapis.com/css2?family=${fam}:ital,wght@0,400;0,700;1,400;1,700&display=swap`
}

document.documentElement.dataset.theme = prefs.theme
