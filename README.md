# Reader — веб-читалка фанфиков (стиль ReadEra)

Self-hosted читалка: скачивает фанфики по ссылке/названию (ficbook, fanfics.me,
author.today, AO3, fanfiction.net), складывает в **Calibre**, читает их в браузере
(Windows/онлайн) с темой как у **ReadEra** и синхронизирует прогресс чтения с
ReadEra на Android. Хостится на VPS рядом с Calibre.

План реализации: `C:\Users\sergy\.claude\plans\synchronous-pondering-sprout.md`.

## Статус по этапам

- [x] **Этап 1 — скелет и читалка.** FastAPI + SQLite, фронтенд на
      [foliate-js](https://github.com/johnfactotum/foliate-js) (EPUB + FB2) с темой
      ReadEra (день/сепия/серая/ночь/чёрная), ручная загрузка файла, библиотека,
      сохранение/восстановление прогресса (ratio + CFI), deep-link `/?open=<id>`.
- [ ] **Этап 2 — скачивание.** FanFicFare (ficbook/fanfics.me/AO3/ffn) + адаптер
      author.today + FicHub-фоллбэк; добавление в Calibre.
- [ ] **Этап 3 — sync с ReadEra.** Чтение/запись бэкапа `.bak` через Google Drive
      (rclone), матч книг по SHA-1, прогресс по `doc_position.ratio`.
- [ ] **Этап 4 — мониторинг.** Шифрованные креды аккаунтов, проверка обновлений
      подписок, авто-докачка в Calibre.
- [ ] **Этап 5 — деплой.** На VPS: `git clone`, venv, systemd, обратный прокси.

## Структура

```
backend/app/        FastAPI: роутеры (library, reader, progress), модели, конфиг
backend/downloaders/  движки скачивания (этап 2)
backend/readera/      sync с ReadEra (этап 3)
backend/accounts/     мониторинг аккаунтов (этап 4)
frontend/           SPA: index.html, css/theme.css, js/app.js
frontend/vendor/foliate-js/  завендоренный движок рендера книг (MIT)
scripts/shoot.mjs   визуальная проверка читалки в браузере (puppeteer-core)
deploy/             .env.example, systemd-юнит (этап 5)
```

## Запуск (локально, Windows)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --port 8123
# открыть http://127.0.0.1:8123/
```

Рантайм-данные (БД, книги) кладутся в `data/` (в `.gitignore`). Переопределяется
переменной `READER_DATA_DIR` и др. — см. `deploy/.env.example`.

## Конфигурация

Все настройки — через переменные окружения (или `.env`). См. `deploy/.env.example`
и `backend/app/config.py`.

## Лицензия

MIT. Завендоренная foliate-js — MIT (см. `frontend/vendor/foliate-js/LICENSE`).
