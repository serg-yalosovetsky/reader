# Configuration

Всё — через переменные окружения (или `.env` в корне). Источник истины —
`backend/app/config.py`. Дефолты рассчитаны на локальный запуск (Windows);
прод-значения — в `/root/reader/.env` на VPS. Пример — `deploy/.env.example`.

## Пути / данные
| ENV | дефолт | смысл |
|---|---|---|
| `READER_DATA_DIR` | `<repo>/data` | корень рантайм-данных (БД, книги, обложки, ключ) |
| `READER_BOOKS_DIR` | `DATA_DIR/books` | файлы книг `<sha1>.<ext>` |
| `READER_COVERS_DIR` | `DATA_DIR/covers` | обложки `<sha1>.<ext>` |
| `READER_DB_PATH` | `DATA_DIR/reader.db` | SQLite |
| `READER_SECRET_KEY_PATH` | `DATA_DIR/secret.key` | ключ Fernet (создаётся, 600) |
| `READER_FRONTEND_DIR` | `<repo>/frontend` | статика SPA |

## Calibre
| ENV | смысл |
|---|---|
| `READER_CALIBREDB_BIN` | путь к `calibredb` (дефолт из PATH) |
| `READER_CALIBRE_LIBRARY` | путь к библиотеке (VPS: `/root/calibre_lib`) |
| `READER_CALIBRE_SERVER_URL/USERNAME/PASSWORD` | Content Server (опц., не основной путь) |

## ReadEra sync
| ENV | смысл |
|---|---|
| `READER_RCLONE_BIN` | `rclone` |
| `READER_READERA_BACKUP_REMOTE` | папка Drive с `.bak` (VPS: `gdrive:ReadEra`) |
| `READER_READERA_BOOKS_REMOTE` | папка книг ReadEra (VPS: `gdrive:ReadEra/Books`) |
| `READER_READERA_SYNC_INTERVAL_MIN` | период авто-импорта (0 = выкл; VPS: 0) |

## Скачивание / мониторинг
| ENV | смысл |
|---|---|
| `READER_FICHUB_API` | `https://fichub.net/api/v0` |
| `READER_DOWNLOAD_CONCURRENCY` | лимит конкурентных скачиваний (1) |
| `READER_MONITOR_INTERVAL_MIN` | период проверки обновлений (0 = выкл; VPS: 360) |

## Прод-`.env` (VPS, ориентир)
```
READER_DATA_DIR=/root/reader/data
READER_CALIBREDB_BIN=calibredb
READER_CALIBRE_LIBRARY=/root/calibre_lib
READER_READERA_BACKUP_REMOTE=gdrive:ReadEra
READER_READERA_BOOKS_REMOTE=gdrive:ReadEra/Books
READER_READERA_SYNC_INTERVAL_MIN=0
READER_MONITOR_INTERVAL_MIN=360
```

## Клиентские настройки (не ENV)
Тема/шрифт/поля/режим/колонки хранятся в `localStorage` браузера (`reader.prefs`),
не на сервере.
