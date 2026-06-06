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

## foliate-js — координаты в scrolled-режиме (критично для TTS)
- `pager.start` = `#container.scrollTop` (смещение от верха документа)
- `pager.size` = высота видимой области (viewport height)
- iframe = вся глава (30 000–50 000 px), `document.scrollTop = 0` всегда
- `elementFromPoint(x, y)` внутри iframe: **document-координаты** (не viewport)
- `getBoundingClientRect()` внутри iframe: относительно iframe viewport (= видимая область)
- Прокрутка: `view.renderer.shadowRoot?.querySelector('#container')?.scrollBy({top, behavior:'smooth'})`

## TTS (Text-to-Speech)
Состояние: `ttsSt` (константа, не реактивная). Весь код в `app.js`.

### Архитектура
- `ttsExtract()` — извлекает текст видимой страницы. Три уровня fallback:
  1. `elementFromPoint`-зонд → видимые блоки `p/div/li/blockquote`
  2. `bookDoc.body.innerText` если блоков < 5 и iframe высокий (EPUB без `<p>`)
  3. Второй fallback если extracted < 300 символов — весь body
  Вырезает «Примечания:» / «Footnotes» и ниже (`TTS_NOTES_RE`), URL, пустые строки.
- `ttsSplit(text)` — режет на чанки по предложениям (не более ~500 символов).
- `ttsSpeakChunk()` — async: `fetch /api/tts/synth` → `new Audio(url)` → `play()` →
  `requestAnimationFrame(ttsWordRaf)`. Хранит prefetch следующего чанка для устранения паузы.
- `ttsWordRaf()` — RAF-цикл подсветки слов + **автопрокрутка**:
  - Подсветка: `findCharRange(bookDoc.body, _chunkBodyOffset + wt.charIndex, wt.charLength)` →
    `CSS.highlights.set('tts-word', new Highlight(range))` (CSS Highlight API)
  - **Автопрокрутка** (scrolled-режим): если `rect.bottom > viewH * 0.82`,
    `#container.scrollBy({top: rect.top - viewH * 0.25, behavior:'smooth'})`
- `ttsStart()` — если `chunks.length > 0 && idx < chunks.length`, возобновляет с
  сохранённой позиции (не перечитывает страницу).
- `ttsStop()` — обнуляет `audio`, сохраняет `chunks`/`idx` (пауза с позицией).

### Вспомогательные функции
- `findTextOffset(el, searchText)` — TreeWalker для поиска char-смещения.
- `findCharRange(root, start, len)` — TreeWalker для Range по char-позиции.
- `ttsClearHighlights()` — очищает `CSS.highlights` и RAF.
- `_chunkBodyOffset` — смещение начала текущего чанка в `bookDoc.body`.

### Автодетект языка
Кириллица → ru-голос из `allVoices`, иначе → en-голос. Дефолт: `xenia`.

### bookCSS()
Содержит `::highlight(tts-word) { background: #e8a000; color: #000 }` —
подсветка текущего слова. Работает только в браузерах с CSS Highlight API (Chrome 105+).

## Библиотека
- `bookCard(w)` — карточка с кнопкой `×` (`.book-del-btn`) в левом верхнем углу обложки.
  Появляется при hover, удаляет книгу через `DELETE /api/library/{id}`.
- Фильтр `#lib-filter` — поиск по заголовку/автору, включая Calibre-карточки.
- Calibre-карточка (`calibreCard`) — бейдж «Calibre», клик = импорт + открытие.

## Навигация (app.js)
- **Зоны клика**: `#tap-prev`/`#tap-next` (по 22% по краям, поверх foliate-view); центр
  свободен для выделения/ссылок.
- **Клавиши** (`handleKey`, на document и на каждом doc книги через `load`): ←/PageUp →
  prev; →/PageDown → next; пробел → next, Shift+пробел → prev; Home/End → 0/1.
- **Колесо прокрутки** в scrolled-режиме: в конце главы → `view.next()`.
- **TOC**: первый элемент → `view.goToFraction(0)` (весь файл с начала).

## Прочие экраны
- Форма «Добавить» (ingest), «Загрузить файл», «⇄ ReadEra» (sync), «↻ Обновления»
  (monitored/check), «Аккаунты» (модал: список/добавление аккаунтов + отслеживаемое).
- deep-link `/?open=<id>` — сразу открыть книгу.
- favicon — `frontend/favicon.svg` (книга в янтарном).
