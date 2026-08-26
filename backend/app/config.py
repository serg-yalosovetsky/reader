"""Конфигурация приложения. Значения читаются из переменных окружения (.env),
с разумными дефолтами для локальной разработки на Windows."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Корень проекта: .../reader
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Каталог рантайм-данных (БД, скачанные файлы, ключ шифрования). В .gitignore.
DATA_DIR = Path(os.getenv("READER_DATA_DIR", PROJECT_ROOT / "data"))
BOOKS_DIR = Path(os.getenv("READER_BOOKS_DIR", DATA_DIR / "books"))
COVERS_DIR = Path(os.getenv("READER_COVERS_DIR", DATA_DIR / "covers"))
TMP_DIR = Path(os.getenv("READER_TMP_DIR", DATA_DIR / "tmp"))
# Кэш синтезированной речи (TTS): <key>.mp3 + <key>.json (пословные тайминги).
TTS_DIR = Path(os.getenv("READER_TTS_DIR", DATA_DIR / "tts"))
# Книги, сконвертированные в EPUB (PDF → EPUB): <sha1>.epub. Оригинал остаётся
# на месте — это производный файл, его можно удалить и пересобрать.
CONVERTED_DIR = Path(os.getenv("READER_CONVERTED_DIR", DATA_DIR / "converted"))
# calibre ebook-convert — конвертер PDF/DJVU/… → EPUB. Пусто => берём из PATH.
EBOOK_CONVERT_BIN = os.getenv("READER_EBOOK_CONVERT_BIN", "ebook-convert")
# Потолок времени одной конвертации (сек): толстый PDF со сканами долгий.
CONVERT_TIMEOUT_SEC = int(os.getenv("READER_CONVERT_TIMEOUT_SEC", "900"))

# БД: только Postgres (mesh-postgres через READER_DB_URL,
# postgresql+psycopg://reader:…@127.0.0.1:5433/reader).
#
# Раньше здесь стоял ТИХИЙ фолбэк на локальный SQLite, и это дорого стоило:
# systemd читает .env через EnvironmentFile, а прямой запуск .venv/bin/python по
# ssh — нет. Без переменной код молча уходил в пустой файл, живой сервис писал в
# Postgres, а отладочный SELECT возвращал пусто — час ложной диагностики «запись
# не доходит до БД». Хуже с cleanup-скриптами: удаляешь из SQLite, а строки
# остаются в проде. Теперь отсутствие READER_DB_URL — ошибка, а не догадка.
#
# SQLite остаётся ровно для тестов: tests/conftest.py снимает READER_DB_URL и
# задаёт READER_DB_PATH во временном каталоге. Поэтому фолбэк разрешён только
# под pytest или при явном READER_ALLOW_SQLITE=1 — то есть когда его попросили,
# а не когда забыли окружение.
DB_PATH = Path(os.getenv("READER_DB_PATH", DATA_DIR / "reader.db"))

_DB_URL = os.getenv("READER_DB_URL")
if not _DB_URL:
    if "pytest" in sys.modules or os.getenv("READER_ALLOW_SQLITE") == "1":
        _DB_URL = f"sqlite:///{DB_PATH.as_posix()}"
    else:
        raise RuntimeError(
            "READER_DB_URL не задан — боевая БД это Postgres, а не SQLite. "
            "systemd берёт переменную из /root/reader/.env; для прямого запуска: "
            "cd /root/reader && set -a; . ./.env; set +a && .venv/bin/python ... "
            "(для тестов на SQLite: READER_ALLOW_SQLITE=1)"
        )
DB_URL = _DB_URL

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
# Кэш файлов книг Calibre (fetch-on-open): книга тянется по требованию сюда,
# кэш вытесняемый (LRU по mtime, лимит MB). Сама книга живёт в Calibre.
CALIBRE_CACHE_DIR = Path(os.getenv("READER_CALIBRE_CACHE_DIR", DATA_DIR / "calibre_cache"))
CALIBRE_CACHE_MAX_MB = int(os.getenv("READER_CALIBRE_CACHE_MAX_MB", "2048"))
# Период catalog-sync каталога Calibre в Work-ссылки (минуты; 0 — выключить).
CALIBRE_SYNC_INTERVAL_MIN = int(os.getenv("READER_CALIBRE_SYNC_INTERVAL_MIN", "0"))

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
FICBOOK_FEED_INTERVAL_MIN = int(os.getenv("READER_FICBOOK_FEED_INTERVAL_MIN", "15"))

# --- Скачивание (этап 2) ---
FICHUB_API = os.getenv("READER_FICHUB_API", "https://fichub.net/api/v0")
DOWNLOAD_CONCURRENCY = int(os.getenv("READER_DOWNLOAD_CONCURRENCY", "1"))

# --- Генерация обложек ИИ (для книг без картинки) ---
# Провайдер: auto | comfy | pollinations | openai. auto = локальный ComfyUI, если
# доступен по COMFY_URL, иначе бесплатный Pollinations.
IMAGE_PROVIDER = os.getenv("READER_IMAGE_PROVIDER", "auto").strip().lower()
IMAGE_GEN_ENABLED = os.getenv("READER_IMAGE_GEN", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)
# Секунд на одну генерацию (FLUX на 3090 ~15-40с; Pollinations ~10-30с).
IMAGE_TIMEOUT = int(os.getenv("READER_IMAGE_TIMEOUT", "120"))
# ComfyUI (SergPC по Tailscale), напр. http://100.104.122.99:8188. Пусто => выкл.
COMFY_URL = os.getenv("READER_COMFY_URL", "").rstrip("/")
COMFY_CKPT = os.getenv("READER_COMFY_CKPT", "flux1-dev-fp8.safetensors")
# OpenAI gpt-image-1 (задел). Ключ берётся из окружения.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# --- Арт-бриф книги (Ollama на SergPC через tailscale serve) ---
# Ollama сводит title/жанры/аннотацию в короткий англ. визуальный бриф, который
# идёт в промпт обложки вместо сырой (часто русской) аннотации. Кешируется в
# Work.cover_brief. Пусто => брифы не генерим (промпт по сырой аннотации).
OLLAMA_URL = os.getenv(
    "READER_OLLAMA_URL", "https://sergpc.tail939af1.ts.net:11434"
).rstrip("/")
OLLAMA_MODEL = os.getenv("READER_OLLAMA_MODEL", "qwen3.5:4b")
BRIEF_ENABLED = os.getenv("READER_BRIEF", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
    "",
)


def ensure_dirs() -> None:
    """Создать рантайм-каталоги при старте."""
    for d in (DATA_DIR, BOOKS_DIR, COVERS_DIR, TMP_DIR, TTS_DIR, CONVERTED_DIR):
        d.mkdir(parents=True, exist_ok=True)
