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
- **Лента** (`flow=scrolled`): одна колонка **во всю ширину**. ВАЖНО (корень давнего бага
  «лента в половину экрана»): в scrolled `#container` (`grid-column:1/-1`) садился только в
  первый трек многотрекового грида полей `#top` и занимал ~половину ширины. Документ книги
  при этом корректен. **Фикс** — в `vendor/foliate-js/paginator.js` шадоу-CSS:
  `:host([flow="scrolled"]) #top { grid-template-columns:1fr; grid-template-rows:1fr }`
  (схлопываем грид в одну ячейку). Плюс `scrolled()` сбрасывает весь колоночный контекст,
  а `app.js` распахивает `html/body` и задаёт поля паддингом.
- Диагностика foliate (scrolled): **скриншоты ненадёжны**, замеряй DOM. Shadow-DOM
  paginator — `closed`; чтобы измерить внутренние узлы, заходи **изнутри iframe книги**
  (`window.frameElement` → вверх по `parentElement`). Рабочий приём — Playwright по
  SSH-туннелю (`ssh -N -L 8124:127.0.0.1:8123`, в обход SSO) + `getComputedStyle`/
  `getBoundingClientRect` (см. историю фикса).

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
