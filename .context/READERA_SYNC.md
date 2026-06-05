# ReadEra Sync

Двусторонняя синхронизация прогресса чтения с ReadEra (Android). Модули —
`backend/readera/`. У ReadEra нет публичного API; работаем через её **ручной бэкап**.

## Почему ручной бэкап (а не авто-синк Premium)
ReadEra Premium синхронит в Google Drive только **книги** (`gdrive:ReadEra/Books/`),
а прогресс/закладки — в скрытый Drive `appDataFolder` (приватен для приложения,
снаружи через rclone/API недоступен). Парсится именно **ручной** бэкап (`.bak`).

## Формат бэкапа (`backup.py`) — проверено на ReadEra Premium, db v110
`.bak` = zip: `library.json`, `meta.json`, `prefs.xml`, `search-history.xml`.
`library.json = {docs, colls, words}`. Каждый doc:
- `uri = "sha-1:<hex>"`, `data.doc_sha1` — **SHA-1 файла = идентичность книги**;
- `data.doc_position` — JSON-строка с **`ratio` (0..1)**, `page`, `pagesCount`, `xPath`, …;
- `data.doc_last_read_time` — epoch ms (для last-write-wins);
- `citations[]` — закладки/цитаты.
- `read_backup(path) → {sha1: ReadEraDoc{ratio,last_read_time,title,citations}}`.
- `patch_backup(src, dst, {sha1:(ratio,lrt)})` — обновляет `doc_position.ratio`,
  `page`, `doc_last_read_time`; прочее (meta/prefs) переносит как есть. xPath не
  пересчитываем (best-effort при restore).

## Транспорт — `gdrive.py` (rclone)
remote `gdrive:` настроен на VPS (`/root/.config/rclone/rclone.conf`, scope drive).
`latest_backup()` (свежайший `*.bak` в `READERA_BACKUP_REMOTE`), `pull/push`,
`push_book` (кладёт скачанную книгу в `READERA_BOOKS_REMOTE` → Premium тянет на телефон).

## Логика — `sync.py`
- **import** (ReadEra → веб): тянем свежий `.bak` → для каждого нашего `Work` матч по
  `sha1`; если `doc_last_read_time` новее нашего `Progress.last_read_time` и `ratio>0` —
  пишем `ratio`, `source=readera`, `locator=""` (CFI нет → восстановление по ratio).
- **export** (веб → ReadEra): где наш веб-прогресс новее — патчим `.bak`, кладём в Drive
  как `ReadEra-restore-<ts>.bak`. Пользователь делает Restore в ReadEra (авто-вливания нет).
- **sync** = import + export. Эндпоинты — `/api/readera/*`; ручная загрузка `.bak` —
  `/api/readera/upload-backup` (без rclone).

## Линчпин
Один и тот же файл (один SHA-1) должен лежать и в ReadEra, и у нас. Поэтому скачанные
книги доставляются в `ReadEra/Books` (Premium → телефон), и `doc_sha1` совпадает с
нашим `Work.sha1`. Без совпадения SHA-1 позиции не сматчатся.

## Восстановление позиции в вебе
`locator` (CFI) — точно; иначе `ratio` → `goToFraction`. Импорт из ReadEra даёт ratio.
