# System Overview

## Назначение

Self-hosted веб-читалка фанфиков и книг с темой как у **ReadEra**. Закрывает то,
что ReadEra (только Android) не умеет: чтение на Windows/онлайн с синхронизацией
прогресса. Три опоры:

1. **Скачать и читать**: вставил ссылку (или платную AT-книгу) → сервер скачивает
   полный текст с поддерживаемых сайтов → открывает в браузерной читалке.
2. **Мониторинг подписок**: логин в аккаунты сайтов (креды шифруются) → фид
   обновлений избранного → авто-докачка новых глав.
3. **Calibre + ReadEra**: книги кладутся в Calibre (на том же VPS); прогресс чтения
   синхронизируется с ReadEra через её бэкап.

Прод: **https://reader.ibotz.fun** (за SSO), бэкенд на VPS рядом с Calibre.

## Модули (карта кода)

```
backend/app/                FastAPI-приложение
  main.py                   сборка app: роутеры + статика + lifespan(scheduler)
  config.py                 все настройки из ENV (дефолты для локалки)
  db/models.py              SQLModel: Work, Progress, Account, Monitored, SyncState
  db/session.py             engine + WAL-прагмы + get_session
  storage.py                файловое хранилище книг по SHA-1
  covers.py                 извлечение/скачивание обложек (EPUB OPF, FB2, og:image)
  crypto.py                 Fernet-шифрование секретов аккаунтов
  services.py               register_download: дедуп + Calibre + ReadEra + обложка
  scheduler.py              APScheduler: периодический мониторинг + ReadEra-импорт
  routers/                  library, reader, progress, ingest, calibre, readera, accounts
backend/downloaders/        движки скачивания
  chain.py                  маршрутизация по домену + фоллбэк платного AT
  fanficfare_engine.py      FanFicFare (subprocess): ficbook/fanfics/AO3/ffn, meta-only
  authortoday.py            свой адаптер author.today (XOR-расшифровка глав)
  readli.py                 readli.net (постранично + slug + поиск)
  searchfloor.py            searchfloor.org (/book/<id> FB2, /b/, boosty, поиск)
  fichub.py                 FicHub API (фоллбэк для англ-сайтов)
  epub_build.py             общий сборщик EPUB из HTML-секций (+ обложка)
  base.py                   DownloadResult, исключения (UnsupportedURL, PaidContentError)
backend/calibre/client.py   calibredb add + чтение metadata.db
backend/accounts/           аккаунты и мониторинг
  store.py                  креды/cookies (шифр.), маппинг хост→сайт
  monitor.py                детект обновлений по числу глав + авто-докачка
  feeds.py                  фиды подписок: ficbook(API)/author.today(/feed)/fanfics
frontend/                   SPA: index.html, css/theme.css, js/app.js
  vendor/foliate-js/        завендоренный движок рендера книг (MIT)
deploy/                     reader.service, nginx-конфиг, .env.example, заметки
```

## Границы рантайма

- **Один процесс**: uvicorn, 1 воркер (RAM-дисциплина VPS). APScheduler — in-process.
- **Тяжёлое — в subprocess**: FanFicFare запускается коротким subprocess'ом на задачу
  (память освобождается сразу).
- **Рендер книг — на клиенте**: foliate-js парсит и рендерит EPUB/FB2 в браузере;
  сервер только отдаёт файл. Серверу почти ничего не стоит.
- **Calibre — на том же хосте**: читаем `metadata.db` напрямую, пишем через `calibredb`.
- **Внешний доступ — только nginx + SSO**: бэкенд слушает `127.0.0.1`.

## Основные потоки

**Скачать → читать (стори №1).** `POST /api/ingest {query}` → `chain.fetch` выбирает
адаптер по домену → скачивает полный текст → `services.register_download` (SHA-1,
дедуп по названию+автор, обложка, добавление в Calibre, копия в ReadEra/Books) →
карточка в библиотеке → открытие во foliate-js. Платная author.today → детект →
фоллбэк поиском на searchfloor/readli (полная бесплатная версия).

**Чтение и прогресс.** Фронт грузит файл (`/api/reader/{id}/file`) во foliate,
восстанавливает позицию (CFI или ratio). Событие `relocate` → дебаунс →
`PUT /api/progress/{id}` (ratio 0..1 + CFI).

**Мониторинг (стори №2).** `monitor.check_all`: `feeds.pull_all` логинится в аккаунты,
тянет фид обновлений подписок → ставит работы на отслеживание; затем для каждого
отслеживаемого фика берёт число глав (FanFicFare `--meta-only`), при росте —
авто-докачивает (обновляя тот же Work). Расписание + кнопка «Обновления».

**Sync с ReadEra (стори №3).** `readera.sync`: rclone тянет ручной бэкап ReadEra
(`.bak`) из Google Drive → матч книг по SHA-1 файла → импорт `doc_position.ratio`
в наш `Progress` (и обратно — патч `.bak` для restore). Скачанные книги кладутся в
`ReadEra/Books` (Premium синхронит на телефон → SHA-1 совпадает).
