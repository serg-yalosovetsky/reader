"""Конфигурация приложения. Значения читаются из переменных окружения (.env),
с разумными дефолтами для локальной разработки на Windows."""
from __future__ import annotations

import os
from pathlib import Path

# Корень проекта: .../reader
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Каталог рантайм-данных (БД, скачанные файлы, ключ шифрования). В .gitignore.
DATA_DIR = Path(os.getenv("READER_DATA_DIR", PROJECT_ROOT / "data"))
BOOKS_DIR = Path(os.getenv("READER_BOOKS_DIR", DATA_DIR / "books"))
TMP_DIR = Path(os.getenv("READER_TMP_DIR", DATA_DIR / "tmp"))

# SQLite-файл
DB_PATH = Path(os.getenv("READER_DB_PATH", DATA_DIR / "reader.db"))
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

# Ключ Fernet для шифрования кредов аккаунтов (этап 4). Файл вне репо.
SECRET_KEY_PATH = Path(os.getenv("READER_SECRET_KEY_PATH", DATA_DIR / "secret.key"))

# Frontend
FRONTEND_DIR = Path(os.getenv("READER_FRONTEND_DIR", PROJECT_ROOT / "frontend"))

# --- Calibre (этап 2) ---
# Путь к calibredb (CLI). Пусто => полагаемся на PATH.
CALIBREDB_BIN = os.getenv("READER_CALIBREDB_BIN", "calibredb")
# Путь к библиотеке Calibre на хосте (локально для calibredb), либо URL Content Server.
CALIBRE_LIBRARY = os.getenv("READER_CALIBRE_LIBRARY", "")
CALIBRE_SERVER_URL = os.getenv("READER_CALIBRE_SERVER_URL", "")
CALIBRE_USERNAME = os.getenv("READER_CALIBRE_USERNAME", "")
CALIBRE_PASSWORD = os.getenv("READER_CALIBRE_PASSWORD", "")

# --- ReadEra sync (этап 3) ---
# rclone-remote и путь к каталогу с бэкапами ReadEra в Google Drive.
RCLONE_BIN = os.getenv("READER_RCLONE_BIN", "rclone")
# Папка Drive, куда пользователь кладёт ручные бэкапы ReadEra (*.bak) и куда мы
# кладём пере-собранный .bak для restore. Напр. "gdrive:ReadEra".
READERA_BACKUP_REMOTE = os.getenv("READER_READERA_BACKUP_REMOTE", "")
# Папка Drive с книгами ReadEra Premium — туда можно класть скачанные книги,
# чтобы ReadEra на телефоне подхватил их авто-синком. Напр. "gdrive:ReadEra/Books".
READERA_BOOKS_REMOTE = os.getenv("READER_READERA_BOOKS_REMOTE", "")
# Период авто-импорта прогресса из бэкапа ReadEra (минуты; 0 — выключить).
READERA_SYNC_INTERVAL_MIN = int(os.getenv("READER_READERA_SYNC_INTERVAL_MIN", "0"))

# --- Мониторинг аккаунтов (этап 4) ---
# Период проверки обновлений отслеживаемых фиков (минуты; 0 — выключить).
MONITOR_INTERVAL_MIN = int(os.getenv("READER_MONITOR_INTERVAL_MIN", "0"))

# --- Скачивание (этап 2) ---
FICHUB_API = os.getenv("READER_FICHUB_API", "https://fichub.net/api/v0")
DOWNLOAD_CONCURRENCY = int(os.getenv("READER_DOWNLOAD_CONCURRENCY", "1"))


def ensure_dirs() -> None:
    """Создать рантайм-каталоги при старте."""
    for d in (DATA_DIR, BOOKS_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
