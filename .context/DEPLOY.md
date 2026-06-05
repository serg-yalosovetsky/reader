# Deploy

Прод — VPS «peaceful-albattani» (Tailscale `100.66.108.118`, публичный
`212.227.115.106`), рядом с Calibre. Подробная разведка — `deploy/VPS-NOTES.md`.

## Доступ к VPS с этой машины (важно)
- **Прямой SSH с Windows**: `ssh serg@100.66.108.118 "<cmd>"` (Windows Tailscale +
  OpenSSH; `serg` = passwordless sudo). Это основной путь.
- Bash-инструмент Claude тут сломан (Cygwin fork-ошибки) — командуй через PowerShell.
- WSL — запасной путь (`wsl.exe -d Ubuntu --exec ssh vps ...`), периодически падает
  в `Wsl/Service/E_UNEXPECTED`.
- Сложное экранирование PowerShell→ssh→bash ломается: JSON-тела писать во временный
  файл и `curl --data @file`; скрипты слать через stdin (`... | ssh vps "python -"`).

## Установка (однократно)
```bash
sudo git clone https://github.com/serg-yalosovetsky/reader.git /root/reader
cd /root/reader
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
# .env (см. CONFIGURATION.md), затем:
sudo cp deploy/reader.service /etc/systemd/system/reader.service
sudo systemctl daemon-reload && sudo systemctl enable --now reader
```

## Сервис
- systemd `reader.service`: uvicorn 1 воркер, бинд **127.0.0.1:8123**, `MemoryMax=400M`,
  enabled. Команды выполняются под root (`sudo bash -c '...'`), т.к. /root и Calibre — root.

## HTTPS + SSO (nginx)
- Домен `reader.ibotz.fun` (wildcard-DNS `*.ibotz.fun` → 212.227.115.106 — резолвится сразу).
- Конфиг — `deploy/nginx-reader.ibotz.fun.conf` (симлинк в `sites-enabled`). Паттерн
  как у `monoflow`: `auth_request` к **vps-sso** (`[::1]:3010`, Clerk) с
  `X-Internal-Secret`; без входа — редирект на `https://sso.ibotz.fun/sign-in`.
- TLS — Let's Encrypt (`certbot certonly --nginx -d reader.ibotz.fun`).
- nginx терминирует TLS и проксирует на `127.0.0.1:8123`.

## rclone / Google Drive
remote `gdrive:` (scope drive) в `/root/.config/rclone/rclone.conf`. Настройка —
`rclone authorize "drive"` на машине с браузером → токен → `rclone config create`.
Папки: `gdrive:ReadEra` (бэкапы), `gdrive:ReadEra/Books` (книги).

## Обновление
```bash
sudo bash -c 'cd /root/reader && git pull && .venv/bin/pip install -r requirements.txt && systemctl restart reader'
```
(pip — только если менялись зависимости.) Для отладки прод-UI без SSO — SSH-туннель:
`ssh -N -L 8124:127.0.0.1:8123 serg@100.66.108.118` → `http://127.0.0.1:8124`.

## RAM-дисциплина
Коробка ~7.9 ГБ, узкое место — RAM. Сервис ~50 МБ; FanFicFare — короткий subprocess.
Тяжёлый компьют — не на VPS.
