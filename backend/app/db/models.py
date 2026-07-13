"""Модели данных (SQLModel / SQLite).

Схема покрывает все этапы плана, но на этапе 1 реально используются Work и Progress.
Account / Monitored задействуются на этапе 4, SyncState — на этапе 3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Work(SQLModel, table=True):
    """Произведение (фанфик/книга), известное читалке."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = ""
    author: str = ""
    # Источник: ficbook | fanfics | authortoday | ao3 | ffn | calibre | upload
    site: str = ""
    source_url: str = ""
    # Файл на диске (EPUB/FB2), который рендерит читалка и который уходит в ReadEra.
    file_path: str = ""
    file_format: str = ""  # epub | fb2
    # SHA-1 файла — линчпин синхронизации с ReadEra (doc_sha1).
    sha1: str = Field(default="", index=True)
    # Привязка к Calibre, если книга добавлена/взята оттуда.
    calibre_id: Optional[int] = Field(default=None, index=True)
    chapters_count: int = 0
    cover_path: str = ""
    # Источник обложки: "" | embedded (из файла) | source (og:image) |
    # description (URL в аннотации) | generated (ИИ) | gen_failed (генерация не
    # удалась — не долбим повторно). generated/gen_failed заменяемы реальной.
    cover_source: str = ""
    # Англ. визуальный арт-бриф (Ollama сводит книгу), кеш для промпта обложки.
    cover_brief: str = ""
    # --- Метаданные для карточки/страницы книги (тянутся 1 раз из epub-opf при
    #     добавлении; бэкфилл существующих — из локального epub, без сети). ---
    description: str = ""  # аннотация (dc:description)
    genres: str = ""  # JSON-массив жанров/меток (dc:subject, очищенные)
    characters: str = ""  # JSON-массив персонажей (если удалось выделить)
    fandom: str = ""  # фандом/вселенная (для кроссоверов)
    series: str = ""  # цикл/серия (fb2 <sequence>, epub calibre:series, AT «Цикл»)
    series_index: int = 0  # номер книги в серии (1,2,3…); 0 — неизвестно
    rating: str = ""  # NC-17 | R | PG-13 | 18+ …
    status: str = ""  # в процессе | завершён
    words: int = 0  # объём в словах (если известно)
    meta_synced: bool = False  # метаданные уже разобраны (чтобы не тянуть снова)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Progress(SQLModel, table=True):
    """Прогресс чтения по произведению. Одна строка на work_id."""

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True, unique=True)
    # Доля прочитанного 0..1 — совместимо с ReadEra doc_position.ratio.
    ratio: float = 0.0
    # Точный локатор для foliate-js (CFI/href#frag) для возврата на место в вебе.
    locator: str = ""
    # Текстовый якорь — первые слова текста вверху экрана. Устойчив к пересборке
    # книги (FanFicFare добавил главы → CFI съезжает на другую секцию), поэтому
    # это основной способ восстановления позиции; locator/ratio — фолбэки.
    text_anchor: str = ""
    # Время последнего чтения (для last-write-wins при sync с ReadEra).
    last_read_time: datetime = Field(default_factory=utcnow)
    # Откуда пришло обновление: web | readera
    source: str = "web"


class Account(SQLModel, table=True):
    """Аккаунт пользователя на сайте-источнике (этап 4). Секрет зашифрован Fernet."""

    id: Optional[int] = Field(default=None, primary_key=True)
    site: str = Field(index=True)
    username: str = ""
    enc_secret: str = ""  # зашифрованный пароль
    cookies: str = ""  # зашифрованные cookie-сессии (опц.)
    last_check: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class Monitored(SQLModel, table=True):
    """Отслеживаемое произведение/подписка (этап 4)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: Optional[int] = Field(
        default=None, foreign_key="account.id", index=True
    )
    work_id: Optional[int] = Field(default=None, foreign_key="work.id", index=True)
    source_url: str = ""
    last_seen_chapters: int = 0
    has_update: bool = False
    last_checked: Optional[datetime] = None
    # Ошибки автодокачки: счётчик подряд неудач (для backoff) и текст последней.
    fail_count: int = 0
    last_error: Optional[str] = None


class SyncState(SQLModel, table=True):
    """Произвольные ключ-значение для состояния sync (этап 3)."""

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class Blacklist(SQLModel, table=True):
    """Удалённые «крестиком» книги: не показывать в библиотеке и не докачивать."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title_norm: str = Field(default="", index=True)
    author_norm: str = Field(default="", index=True)
    source_url: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Bookmark(SQLModel, table=True):
    """Закладка в книге. Много закладок на work_id (в отличие от Progress)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True)
    # Доля 0..1 — для сортировки списка и совместимости с ratio.
    ratio: float = 0.0
    # Точный локатор (Readium/foliate JSON-строка) для перехода.
    locator: str = ""
    # Необязательная подпись (например, первые слова абзаца).
    label: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Highlight(SQLModel, table=True):
    """Выделение/цитата в книге. Много на work_id (как Bookmark)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    work_id: int = Field(foreign_key="work.id", index=True)
    # Доля 0..1 — сортировка списка и кросс-девайс якорь.
    ratio: float = 0.0
    # Точный локатор выделения (Readium/foliate JSON-строка) для перехода/рендера.
    locator: str = ""
    # Выделенный текст — для списка цитат и поиска.
    text: str = ""
    # Цвет подсветки (yellow|green|blue|pink…).
    color: str = "yellow"
    created_at: datetime = Field(default_factory=utcnow)
