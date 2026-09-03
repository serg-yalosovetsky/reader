// Настройки вида (localStorage): тема/шрифт/поля/режим/колонки.
const PREFS_KEY = 'reader.prefs'
// Версия схемы настроек. Нужна ровно там, где ЗНАЧЕНИЕ меняет смысл: уровни
// полей — числа, и вставка нового уровня в середину сдвигает все сохранённые.
const PREFS_VERSION = 2
const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}')
export const prefs = Object.assign(
  { theme: 'day', fontScale: 1, marginLevel: 2, fontFamily: 'inter', flow: 'paginated', columns: 1, v: PREFS_VERSION },
  saved,
)
if (prefs.fontFamily === 'sans') prefs.fontFamily = 'open-sans'
export const savePrefs = () => localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))

// Уровень полей -> max-inline-size (меньше значение = уже колонка = шире поля).
export const MARGIN_INLINE = { 0: 760, 1: 690, 2: 620, 3: 480 }
// Уровень 0 — это не «узкие поля», а их отсутствие: текст идёт от края до края.
// Поэтому шкала называет вещи своими именами, а имя «узк.» досталось новому
// промежуточному уровню, которого и не хватало между 0% и 5%.
export const MARGIN_NAME = { 0: 'нет', 1: 'узк.', 2: 'сред.', 3: 'шир.' }
export const MARGIN_MAX = 3
// Боковой отступ книги в процентах ширины и внутренний margin постраничного
// режима — по тому же уровню. Держим рядом со шкалой, чтобы при добавлении
// уровня не забыть ни одну из трёх таблиц (раньше две из них лежали в
// reader-core.js двумя одинаковыми литералами).
export const MARGIN_SIDE_PCT = { 0: 0, 1: 2.5, 2: 5, 3: 12 }
export const MARGIN_GAP = { 0: 0, 1: 8, 2: 16, 3: 34 }

// Миграция сохранённых настроек на текущую схему.
function migratePrefs() {
  if (saved.v === PREFS_VERSION) return
  // v1 -> v2. Шкала полей была {0: нет, 1: сред., 2: шир.}, между 0 и 1
  // вставлен новый уровень: без сдвига сохранённое «сред.» стало бы узким.
  if (typeof saved.marginLevel === 'number') {
    prefs.marginLevel = { 0: 0, 1: 2, 2: 3 }[saved.marginLevel] ?? 2
  }
  // Шрифты с засечками убраны из читалки: старый выбор ведёт в никуда и книга
  // открылась бы системным запасным шрифтом.
  if (['merriweather', 'lora', 'pt-serif', 'georgia', 'serif'].includes(saved.fontFamily)) {
    prefs.fontFamily = 'inter'
  }
  prefs.v = PREFS_VERSION
  savePrefs()
}
migratePrefs()
// Только гротески. Отбор не по вкусу: у каждого проверены КИРИЛЛИЦА и
// НАСТОЯЩИЙ курсив (в художественной прозе он на каждой странице, а
// синтетический наклон её портит). Из-за второго условия сюда не попали
// Golos Text, Manrope, Onest и Commissioner — Google отдаёт их без italic.
export const FONT_STACKS = {
  'inter':         '"Inter", system-ui, sans-serif',
  'noto-sans':     '"Noto Sans", system-ui, sans-serif',
  'open-sans':     '"Open Sans", system-ui, sans-serif',
  'pt-sans':       '"PT Sans", system-ui, sans-serif',
  'ibm-plex-sans': '"IBM Plex Sans", system-ui, sans-serif',
  'rubik':         '"Rubik", system-ui, sans-serif',
  'mulish':        '"Mulish", system-ui, sans-serif',
  'nunito':        '"Nunito", system-ui, sans-serif',
}
// Имя семейства у Google Fonts для каждого нашего ключа. Georgia здесь нет
// намеренно: она системная, её грузить неоткуда и незачем.
export const GFONT_NAMES = {
  'inter':         'Inter',
  'noto-sans':     'Noto Sans',
  'open-sans':     'Open Sans',
  'pt-sans':       'PT Sans',
  'ibm-plex-sans': 'IBM Plex Sans',
  'rubik':         'Rubik',
  'mulish':        'Mulish',
  'nunito':        'Nunito',
}

// URL шрифта ТОЛЬКО для выбранного семейства.
// Раньше здесь была одна константа на все шесть семейств по четыре начертания —
// 24 файла со стороннего хоста в каждом документе книги. Читатель видит одно
// семейство, а ждал загрузки всех: рендер книги гейтится на document.fonts.ready.
// Georgia тут больше нет не потому что системная, а потому что с засечками:
// в списке читалки таких шрифтов не осталось вовсе.
export function gfontsFor(family) {
  const name = GFONT_NAMES[family]
  if (!name) return ''   // системный шрифт — сеть не нужна вовсе
  const fam = name.replace(/ /g, '+')
  return `https://fonts.googleapis.com/css2?family=${fam}:ital,wght@0,400;0,700;1,400;1,700&display=swap`
}

document.documentElement.dataset.theme = prefs.theme
