# Reader #2802 / #2803 — отчёт

Ветка: `codex/reader-2803`. Работа выполнена только в локальной копии; `/opt/reader` и прод-БД не использовались.

## Изменения

1. Карточка `GET /api/library/{id}` отдаёт `file_rev` выбранного файла (в том числе готовой EPUB-конверсии и существующего кеша Calibre). Файловый endpoint отдаёт `X-Book-Rev`, сохраняет `X-Book-SHA1` и ETag, включая ответы 304. SHA-1 берётся из content-addressed имени или кешируется по пути, mtime_ns и размеру. Устаревший Work.sha1 больше не используется вместо фактической ревизии файла.
2. В `reader:offline-meta` записывается `rev`. Перед открытием копии выполняется сетевой запрос карточки с `no-store`: неизвестная/иная ревизия вызывает перекачку. Проверка ограничена 1500 мс; при таймауте/ошибке доступна прежняя копия с пометкой «копия может отставать». Неудачная перекачка не удаляет прежнюю копию. SW пропускает `no-store` в сеть для файла и карточки; SHELL_CACHE повышен v15 → v16.
3. Live-PUT со старой непустой ревизией получает 409, если сохранённая позиция принадлежит текущему файлу. Тело содержит сохранённую позицию и file_rev, без изменения БД. Replay по saved_at проверяется раньше и сохраняет прежнюю семантику. Клиент удаляет отвергнутую запись из очереди, блокирует сохранения на время восстановления, обновляет копию и восстанавливает именно позицию из ответа сервера.
4. При смене главы или скачке доли сохраняется заменяемая позиция с reason=overwrite, book_rev и семейством UA писателя. Для фоновых перезаписей указывается server/readera. Охвачены PUT, импорт ReadEra, слияние дублей и пересчёт доли после роста книги. Новые колонки PositionHistory добавляет существующая автоматическая ADD COLUMN миграция.
5. Причина пустого прохода каждые 15 минут — `_ficbook_feed_job`, а не второй экземпляр общего монитора. После pull_all он вызывал check_all(only_pending=True) даже без has_update. Теперь пустая докачка пропускается. Сам job нужен: быстро подхватывает обновления ленты Ficbook; полный монитор имеет отдельный интервал MONITOR_INTERVAL_MIN. Вывод сделан по коду, без проверки логов/конфигурации прода.

## Проверки

- `node tests/js/test_offline_revision.mjs`: PASS. Отставшая/неизвестная/совпадающая ревизия, таймаут, отсутствие сети, неудачная перекачка с сохранением старого файла; сетевой bypass SW; 409 передаёт серверную позицию и не остаётся в replay.
- Целевые серверные тесты без TestClient: **6 passed, 13 deselected**. Проверены транзакция 409, replay, история главы/устройства/ревизии, карточка и файловый заголовок, кеш SHA-1, ADD COLUMN и пустой job; дополнительно пройден тест вытеснения кеша Calibre.
- `node --check` для изменённых JS/SW и `git diff --check`: PASS.
- Полный pytest запущен: **569 collected**, зависание на первом `tests/test_api.py::test_health` в fixture client (Starlette TestClient.__enter__ → AnyIO start_task_soon/run_sync_from_thread). В потоке event loop — selectors.select. Прогон завершён watchdog с **exit 124**, итоговой зелёной сводки нет.
- Обычный запуск venv также зависает на startup-hook; для диагностических запусков использован системный Python с -S, библиотеками существующего venv и отключённым autoload сторонних pytest-плагинов. Файлы venv не менялись.
- Запуск тестов вне sandbox отклонён инструментом (`rejected by user`).

## Что не проверено / готовность

Критерий готовности НЕ достигнут: нет зелёного полного pytest. HTTP-интеграционные тесты, включая новые тесты в tests/test_progress.py, требуют повторного прогона в среде с работающим TestClient. Живой browser-сценарий не запускался: в доступном venv отсутствует Playwright; использован разрешённый брифом клиентский unit-тест. Не проверялись деплой, реальный телефон/PWA, поведение обновления уже установленного SW, PostgreSQL и продовые логи. Новые столбцы проверены на SQLite.

## Файлы

- backend/app/book_revision.py
- backend/app/db/models.py
- backend/app/routers/library.py
- backend/app/routers/progress.py
- backend/app/routers/reader.py
- backend/app/scheduler.py
- backend/app/services.py
- backend/calibre/sync.py
- backend/readera/sync.py
- frontend/js/core/offline.js
- frontend/js/core/progress-sync.js
- frontend/js/reader-core.js
- frontend/sw.js
- tests/test_progress.py
- tests/test_scheduler_pending.py
- tests/js/test_offline_revision.mjs
- docs/reader-2803-report.md

Коммит не создан: запрос `git add` для перечисленных файлов отклонён инструментом (`rejected by user`). Git-метаданные worktree доступны только для чтения в sandbox. Изменения остаются в рабочем дереве ветки `codex/reader-2803`.

Исходный BRIEF-2803.md не изменялся. `pytest-of-root/` — незакоммиченные артефакты локального прогона.
