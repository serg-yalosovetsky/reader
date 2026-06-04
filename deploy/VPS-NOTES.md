# VPS «peaceful-albattani» — топология для деплоя читалки

Развед-данные (на 2026-06-04). Без секретов. Подробности доступа — в скилле `vps`.

## Доступ из локальной машины (SergPC, Windows)

Bash-инструмент Claude на этой машине сломан (Cygwin fork-ошибки `0xC0000142`),
а login-shell WSL (`bash -lc`) тяжёлый (sshfs-guard в `/etc/profile`). Рабочий путь —
**через PowerShell + WSL без login-shell**:

```powershell
wsl.exe -d Ubuntu --exec ssh vps "<команда>"
```

SSH-алиас `vps` живёт в `~/.ssh/config` WSL Ubuntu (Tailscale SSH, без пароля).

## Calibre

- **Библиотека:** `/root/calibre_lib` (там `metadata.db` и файлы книг).
- **CLI:** `/usr/bin/calibredb` → добавление: `calibredb add --with-library /root/calibre_lib <файл>`.
- **Calibre-Web (`cps`):** слушает `:8083` (есть systemd-юнит `calibre.service`,
  запуск из `/root/calibre/.venv`). Перед ним **nginx** на `:8080`.
- Чтение библиотеки для нашей читалки: проще всего читать `metadata.db` (SQLite)
  напрямую и отдавать файлы из `/root/calibre_lib` — приложение на том же хосте.
- ⚠️ `calibredb add` при запущенном `cps` использует тот же `metadata.db`;
  Calibre-Web подхватывает изменения (возможно, нужна переиндексация/reconnect).

## rclone (для sync с ReadEra, этап 3)

- `/usr/bin/rclone` установлен, но **remote'ов пока нет**.
- Для синхронизации прогресса с ReadEra нужно настроить remote на Google Drive
  Сергея (там ReadEra Premium хранит бэкап `.bak`), напр. `gdrive:` →
  `READER_READERA_BACKUP_REMOTE=gdrive:ReadEra`.

## Деплой (этап 5)

`git clone https://github.com/serhii-yalosovetskyi/reader.git` на VPS → venv →
`.env` (см. `.env.example`) → systemd-юнит → проксирование через nginx.
Порт наружу — **согласовать с человеком** (прод-правило безопасности VPS).
RAM коробки ~7.9 ГБ, держать стек лёгким (uvicorn 1 воркер, FanFicFare — subprocess).
