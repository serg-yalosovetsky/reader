# API Contracts

Все под `/api`. Наружу — за nginx+SSO (заголовки `X-Forwarded-Email` и т.п.).
Блокирующие эндпоинты (`ingest`, `monitored/check`, `readera/*`) синхронные →
FastAPI threadpool. Источник — `backend/app/routers/`.

## Библиотека — `library.py`
- `GET    /api/library` → `[Work]` (новые сверху).
- `GET    /api/library/{id}` → `Work` (404 если нет).
- `POST   /api/library/upload` (multipart `file`) → `Work`. Только .epub/.fb2; дедуп по SHA-1.
- `DELETE /api/library/{id}` → `{deleted}`. Удаляет Work, прогресс, мониторинг, файл и обложку.
- `POST   /api/library/maintenance` → `{removed_duplicates, removed_monitored, covers_added}`.
  Дедуп книг (оставляет самый полный файл), чистка мониторинга, бэкафилл обложек.

## Чтение — `reader.py`
- `GET /api/reader/{id}/file` → бинарь книги (media epub/fb2). foliate грузит на клиенте.
- `GET /api/reader/{id}/cover` → файл обложки (404 если нет → фронт рисует заглушку).

## Прогресс — `progress.py`
- `GET /api/progress/{work_id}` → `Progress` (пустой, если не открывалась).
- `PUT /api/progress/{work_id}` `{ratio:0..1, locator}` → `Progress` (source=web).

## TTS — `tts.py`
- `GET  /api/tts/voices` → `{voices:[{id,name,lang}], default:"xenia"}`.
- `POST /api/tts/synth` `{text, voice, rate}` → `{audio_url, words}`.
  `words = [{t:ms, d:ms, text, charIndex, charLength}]` — пословные тайминги.
  Кеш по SHA-1(voice|rate|text) → `.wav`/`.mp3` + `.json` в `data/tts/`.
- `GET  /api/tts/audio/{key}` → бинарь WAV (Silero) или MP3 (edge-tts).

**Движки:**
- **Silero** (ru-RU): aidar / baya / kseniya / xenia (дефолт) / eugene.
  Локальный PyTorch (venv: torch, soundfile, omegaconf). Тайминги — пропорциональные.
- **edge-tts** (en-US/en-GB/uk-UA): jenny / guy / aria / sonia / polina / ostap.
  Точные тайминги от WordBoundary.

## Скачивание — `ingest.py`
- `POST /api/ingest` `{query}` → `Work`. query = URL (поиск по названию — TODO).
  Подставляет креды аккаунта для домена; ставит фик на мониторинг.
  Ошибки/требование логина → `422`. Платная AT → фоллбэк (см. DOWNLOADERS).

## Calibre — `calibre.py`
- `GET  /api/calibre/status` → `{configured}`.
- `GET  /api/calibre/books` → список книг библиотеки Calibre (из metadata.db).
- `GET  /api/calibre/{id}/cover` → обложка книги из Calibre (cover.jpg).
- `POST /api/calibre/import/{calibre_id}` → `Work` (копия в хранилище + обложка если есть).

## ReadEra sync — `readera.py`
- `GET  /api/readera/status` → `{available, latest_backup}`.
- `POST /api/readera/sync` → `{import, export}` (полная синхронизация).
- `POST /api/readera/import` → импорт прогресса из бэкапа.
- `POST /api/readera/export` → патч `.bak` + выгрузка `ReadEra-restore-*.bak` в Drive.
- `POST /api/readera/upload-backup` (multipart `file`) → импорт из загруженного `.bak`
  (фоллбэк без rclone).

## Аккаунты и мониторинг — `accounts.py`
- `GET    /api/accounts` → `[{id, site, username, last_check}]` (без секретов).
- `POST   /api/accounts` `{site, username, password}` → `{id, site, username}` (шифрует).
- `DELETE /api/accounts/{id}` → `{deleted}`.
- `GET    /api/monitored` → `[{id, source_url, title, last_seen_chapters, has_update, last_checked}]`
  (дедуп отображения).
- `POST   /api/monitored` `{url}` → поставить фик на отслеживание.
- `POST   /api/monitored/check` → `{checked, with_updates, downloaded, feeds, details}`.
  Тянет фиды подписок → детект новых глав → авто-докачка.

## Прочее
- `GET /api/health` → `{status:"ok"}`.
- `GET /` → `index.html` с `Cache-Control: no-cache` (правки UI без хард-рефреша);
  остальная статика — штатно из `frontend/`.
