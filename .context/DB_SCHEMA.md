# DB Schema

SQLite через SQLModel. Определения — `backend/app/db/models.py`. Таблицы создаются
`init_db()` при старте. Включён WAL (`db/session.py`). Файл — `data/reader.db`.

## Work — произведение/книга в библиотеке
| поле | тип | смысл |
|---|---|---|
| id | int PK | |
| title, author | str | метаданные |
| site | str | источник: ficbook\|fanfics\|authortoday\|ao3\|ffn\|readli\|searchfloor\|calibre\|upload |
| source_url | str | URL источника (ключ дедупа/обновления) |
| file_path | str | путь к файлу в `data/books/<sha1>.<ext>` |
| file_format | str | epub\|fb2 |
| **sha1** | str (index) | SHA-1 файла — идентичность книги (= ReadEra `doc_sha1`) |
| calibre_id | int? (index) | id в Calibre, если добавлена |
| chapters_count | int | число глав (для детекта обновлений) |
| cover_path | str | путь к обложке `data/covers/<sha1>.<ext>` |
| created_at, updated_at | datetime | |

## Progress — прогресс чтения (1 строка на work_id)
| поле | тип | смысл |
|---|---|---|
| work_id | int FK unique | |
| **ratio** | float 0..1 | доля прочитанного (совместимо с ReadEra `doc_position.ratio`) |
| locator | str | точный CFI для foliate (приоритет при восстановлении; пусто при импорте из ReadEra) |
| last_read_time | datetime | для last-write-wins при sync |
| source | str | web\|readera |

## Account — аккаунт сайта (мониторинг)
| поле | тип | смысл |
|---|---|---|
| site | str (index) | ficbook\|fanfics\|authortoday\|ao3\|ffn |
| username | str | |
| **enc_secret** | str | пароль, зашифрован Fernet |
| **cookies** | str | cookie-сессия, зашифрована (author.today) |
| last_check | datetime? | |

## Monitored — отслеживаемое произведение/подписка
| поле | тип | смысл |
|---|---|---|
| account_id | int? FK | |
| work_id | int? FK | связанный Work (если скачан) |
| source_url | str | URL фика (идемпотентность add_monitor) |
| last_seen_chapters | int | последнее известное число глав |
| has_update | bool | есть непросмотренное обновление |
| last_checked | datetime? | |

## SyncState — произвольные ключ-значение
`key` (PK), `value`, `updated_at` — состояние sync (mtime бэкапа и т.п.).

## Восстановление позиции (важно)
Открытие книги: если `Progress.locator` (CFI) есть → точное восстановление;
иначе если `ratio>0` → `view.goToFraction(ratio)` (например, импорт из ReadEra,
где CFI нет); иначе — начало текста.
