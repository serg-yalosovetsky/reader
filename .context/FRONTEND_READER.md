# Frontend & Reader

SPA без сборки: `frontend/index.html` + `css/theme.css` + `js/app.js` + завендоренный
`vendor/foliate-js`. Раздаётся статикой FastAPI. `index.html` отдаётся с
`Cache-Control: no-cache` (правки UI подхватываются без хард-рефреша).

## foliate-js (рендер книг)
- `import '/vendor/foliate-js/view.js'` → кастом-элемент `<foliate-view>`.
- Открытие: `view.open(File)` (грузим бинарь через `/api/reader/{id}/file`, оборачиваем
  в `File` с именем `book.<fmt>` — детект FB2 по имени/типу). Затем
  `view.init({lastLocation})` — CFI или начало.
- События: `relocate` (`detail.fraction` 0..1, `detail.cfi`) → дебаунс →
  `PUT /api/progress`; `load` (`detail.doc`) → вешаем клавиши на документ секции.
- Навигация: `view.prev()/next()`, `goToFraction(r)`, `goTo(cfi/href)`.
- **Shadow DOM у foliate-view и paginator — closed**: внутрь не заглянуть из вне
  (важно при отладке вёрстки — измерить/стилизовать внутренние элементы нельзя;
  правим вендоренный код напрямую).

## Темы и настройки (localStorage `reader.prefs`)
Палитры на `html[data-theme]`: day/sepia/grey/night/black (CSS-переменные в theme.css).
Применение к тексту книги — `view.renderer.setStyles(bookCSS())`: цвета берём из
вычисленных CSS-переменных (внутри iframe их нет — подставляем литералы), плюс
размер шрифта, шрифт (serif/sans), межстрочный, выравнивание.
Поля/режим/колонки — атрибуты рендерера (`flow`, `max-inline-size`, `max-column-count`, `gap`).

## Режимы
- **Страницы** (`flow=paginated`): `max-column-count` = 1 (по умолч., как ReadEra) или 2;
  ширина колонки от уровня «Поля» (`max-inline-size`).
- **Лента** (`flow=scrolled`): одна колонка **во всю ширину**. ВАЖНО: foliate в scrolled
  жёстко ставил `body { max-width: columnWidth }` (узкая колонка слева) — **пропатчено**
  в `vendor/foliate-js/paginator.js` (`scrolled()`: `body max-width:none; margin:0`).
  Headless-скриншоты foliate для scrolled НЕНАДЁЖНЫ — проверять в реальном браузере.

## Навигация (app.js)
- **Зоны клика**: `#tap-prev`/`#tap-next` (по 22% по краям, поверх foliate-view); центр
  свободен для выделения/ссылок.
- **Клавиши** (`handleKey`, на document и на каждом doc книги через `load`): ←/PageUp →
  prev; →/PageDown → next; пробел → next, Shift+пробел → prev; Home/End → 0/1.

## Библиотека и обложки
- Карточки: `bookCard` — обложка `<img src="/api/reader/{id}/cover" onerror="this.remove()">`
  поверх заглушки с названием (если обложки нет/ошибка — видна заглушка).
- Прогресс-бар по `ratio`.

## Прочие экраны
- Форма «Добавить» (ingest), «Загрузить файл», «⇄ ReadEra» (sync), «↻ Обновления»
  (monitored/check), «Аккаунты» (модал: список/добавление аккаунтов + отслеживаемое).
- deep-link `/?open=<id>` — сразу открыть книгу.
- favicon — `frontend/favicon.svg` (книга в янтарном).
