# Tech Stack

## Языки и рантайм

- **Python 3.11+** (на VPS 3.12). Один процесс **uvicorn**, 1 воркер.
- **JavaScript (ES-модули)**, без сборки — фронтенд раздаётся статикой.

## Backend

- **FastAPI** — HTTP API. Эндпоинты-обработчики синхронные (`def`) там, где есть
  блокирующий I/O (скачивание, subprocess) — FastAPI выполняет их в threadpool,
  event loop не стопорится.
- **SQLModel** (поверх SQLAlchemy) + **SQLite**. Включён **WAL + busy_timeout**
  (см. `db/session.py`) — иначе долгий мониторинг + параллельные запросы фронта
  дают `database is locked`.
- **APScheduler** (BackgroundScheduler, in-process, без Redis) — периодические
  задачи (мониторинг, ReadEra-импорт). Включается интервалами в ENV.
- **cryptography (Fernet)** — шифрование паролей/cookies аккаунтов. Ключ —
  `data/secret.key` (вне репо, 600).
- **httpx** — HTTP-клиент (адаптеры, FicHub, обложки).
- **beautifulsoup4 + lxml** — парсинг страниц (author.today/readli/searchfloor/фиды).
- **FanFicFare** — движок скачивания для ficbook/fanfics.me/AO3/ffn. Запускается
  **в subprocess** (`python -c "from fanficfare.cli import main; main()"`).
- **edge-tts** (pip) — TTS для en/uk голосов (WordBoundary тайминги).
- **Silero TTS** (torch + soundfile + omegaconf) — локальный PyTorch-синтез для ru-голосов. Модель v4_ru (~130 MB), кеш: /root/.cache/torch/hub/.
- **EbookLib** — сборка EPUB из HTML-секций (адаптеры author.today/readli/boosty).
- **cloudscraper** — обход анти-бота **ficbook** (DDoS-Guard «Проверка безопасности»);
  httpx/обычный requests с дата-центрового IP получают 403.

## Frontend

- **foliate-js** (johnfactotum, MIT) — завендорен в `frontend/vendor/foliate-js`.
  Рендерит **EPUB и FB2** (FB2 критичен для рус-фанфиков). Кастом-элемент
  `<foliate-view>`; события `relocate` (fraction+CFI), `load` (doc секции).
- Свой UI: `index.html` + `css/theme.css` (палитры ReadEra) + `js/app.js`.
  Без фреймворка, ванильный JS.

## Внешняя инфраструктура (VPS)

- **Calibre**: библиотека `/root/calibre_lib`, CLI `calibredb`, Calibre-Web `cps`
  на :8083. Читаем `metadata.db` (SQLite, ro) напрямую; пишем через `calibredb add`.
- **rclone** → Google Drive (remote `gdrive:`, scope drive) — бэкап ReadEra и
  доставка книг в `ReadEra/Books`.
- **nginx + vps-sso** (Clerk) — HTTPS и аутентификация (см. `DEPLOY.md`).

## Непреложные конвенции

- **Бэкенд слушает только `127.0.0.1`** — наружу исключительно через nginx+SSO.
- **Тяжёлый/блокирующий код — в subprocess или threadpool**, не в event loop.
- **SHA-1 файла — идентичность книги** между нашим хранилищем, Calibre и ReadEra
  (линчпин sync). Имя файла в хранилище = `<sha1>.<ext>`.
- **Комментарии и сообщения пользователю — на русском** (см. `CODE_STYLE.md`).
- **Секреты — только Fernet-шифрованные в БД**; Bitwarden НЕ используется (решение).
- **Анти-бот ficbook — всегда через cloudscraper** (фид и скачивание).
