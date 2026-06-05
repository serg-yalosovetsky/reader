# Monitoring (аккаунты и обновления)

Стори №2: следить за обновлениями подписок и авто-докачивать. Модули —
`backend/accounts/`.

## Аккаунты — `store.py`
- `upsert_account(site, username, password)` — пароль шифруется Fernet (`crypto.py`).
- `creds_for_site/creds_for_host`, `get_cookies/set_cookies` (cookies шифруются),
  `touch_check`.
- `site_of_host`: хост → ключ сайта (`_SITE_BY_HOST`): ficbook/fanfics/ao3/ffn/
  authortoday/readli/searchfloor. ВАЖНО: ключ `authortoday` (не `author.today`).
- Пароли наружу не отдаются (`/api/accounts` без секретов).

## Фиды подписок — `feeds.py`
`pull_all(session)`: для каждого аккаунта логин + забор фида обновлений →
`monitor.add_monitor(url)` по каждой работе. Адаптеры:
- **ficbook** (`_ficbook_feed`): **cloudscraper** (анти-бот) → `POST /login_check_static`
  → `POST /user_notifications/get_new` → notifications с `url=/readfic/<id>`
  (type 17 = обновления избранных авторов). get_new **эфемерный** (только непрочитанные).
- **author.today** (`_at_feed`): если есть cookie-сессия — используем её (без входа);
  иначе вход через `loginForm` (`__RequestVerificationToken` из формы, ответ JSON
  `isSuccessful`; вход с нового устройства требует email-код — см. ADR-012). Затем
  `GET /feed` → `article.feed-row` «обновил произведение»/«опубликовал новое» → `/work/<id>`.
- **fanfics** (`_fanfics_feed`): логин `POST /autent.php` работает; страница подписок —
  TODO (заглушка `[]`).

## Детект обновлений и докачка — `monitor.py`
`check_all(session, auto_download=True)`:
1. `feeds.pull_all` — фиды → новые работы в `Monitored`.
2. Для каждого `Monitored`: текущее число глав через `fff.get_meta` (meta-only, без
   логина; ficbook — cloudscraper). Если > `last_seen_chapters` → `has_update=True`,
   и при auto_download — `chain.fetch` + `register_download` (обновляет тот же `Work`
   по `source_url`, не плодит дубли). Пауза 0.3с (вежливость).
3. `list_monitored` — для UI, с дедупом отображения.

Запуск: кнопка «↻ Обновления» (главная и модал «Аккаунты») → `POST /api/monitored/check`;
плюс расписание (`READER_MONITOR_INTERVAL_MIN`, VPS=360) через `app/scheduler.py`.

## Надёжность
- Детект по числу глав устойчивее парсинга залогиненных страниц и работает без логина.
- Долгий `check_all` + параллельные запросы фронта → нужен **WAL** (иначе
  `database is locked`); включён в `db/session.py`.
