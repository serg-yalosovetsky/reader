# Downloaders

Скачивание превращает ссылку в файл книги (EPUB/FB2) + метаданные (`DownloadResult`,
`base.py`). Регистрацию (хранилище/Calibre/ReadEra/дедуп/обложка) делает
`app/services.register_download`.

## Маршрутизация — `chain.fetch(query, creds=None)`
По хосту:
1. `author.today` → `authortoday.download`; при `PaidContentError` → `_fallback_free`.
2. `readli.net` → `readli.download`.
3. `searchfloor.org` → `searchfloor.download`.
4. известный FanFicFare-домен → `fanficfare_engine.download`.
5. иначе → FanFicFare, при `UnsupportedURL` → `fichub.download`.
`creds` (для домена) пробрасываются в FanFicFare (закрытый/18+).

## FanFicFare — `fanficfare_engine.py`
- `download(url)` и `get_meta(url)` (meta-only, для детекта обновлений) — **subprocess**
  `python -c "from fanficfare.cli import main; main()"`, изоляция памяти.
- Флаги: `-f epub --non-interactive -o is_adult=true -o include_images=true`
  (`include_images` → встроенная обложка сайта), `output_filename=book.epub`.
- **ficbook** → добавляется `-o use_cloudscraper=true` (анти-бот). Креды → `-o username/password`.
- Поддержанные домены см. `KNOWN_DOMAINS`.

## author.today — `authortoday.py` (порт юзерскрипта AuthorTodayExtractor)
- Метаданные со страницы `/work/<id>`; список глав из `/reader/<id>` (regex `chapters:[...]`).
- Текст главы: `GET /reader/<id>/chapter?id=<cid>`, тело `data.text` **зашифровано**,
  ключ в заголовке `reader-secret`. Расшифровка: `key = reverse(secret)+"@_@"+userId`,
  XOR посимвольно (аноним → `userId=""`).
- Ретраи на сетевые сбои и пустой текст (троттлинг), пауза между главами.
- Сборка EPUB через `_build_epub` (NCX-оглавление, встроенная обложка из og:image).
- **Платная книга** (нет «Свободный доступ») → `PaidContentError(title, author)`.

## readli.net — `readli.py`
Постранично: `/chitat-online/?b=<id>&pg=N`. Число страниц — из `<title>` «N/M».
Текст — `div.reading__text`. Поддержка slug-URL (`/<slug>/` → находит ссылку на читалку)
и `search_and_download(title)`. Сборка через общий `epub_build`.

## searchfloor.org — `searchfloor.py`
- `/book/<id>` — **прямое скачивание** полного **FB2.zip** → распаковка в `.fb2` (основной путь).
- `/b/<id>` — страница книги (берём id, качаем `/book/<id>`).
- `/boosty/post/<id>` — пост Boosty (`#postContent` → EPUB).
- `search_book(title)` — поиск (`/search?q=`) → `/b/<id>` (используется фоллбэком).

## Фоллбэк платного AT — `chain._fallback_free(title, author)`
searchfloor.search_book → `_download_book` (полный FB2) → если нет, readli.search_and_download.
Иначе — понятная ошибка.

## FicHub — `fichub.py`
Фоллбэк для англ-сайтов без адаптера: `GET {FICHUB_API}/epub?q=URL` → ссылка на EPUB → скачать.

## Общий сборщик — `epub_build.build_epub(...)`
HTML-секции → EPUB. **Только NCX** (EpubNav в ebooklib падает на page-list при
служебных документах). Тело секции всегда непустое. Опц. `cover` → `set_cover`.
ВАЖНО: в `EpubHtml.content` НЕ класть `<?xml?>`-декларацию — иначе ebooklib пишет пустой файл.

## Анти-бот
ficbook — DDoS-Guard; с VPS только cloudscraper. author.today/fanfics/readli/searchfloor —
обычный httpx с браузерным UA.
