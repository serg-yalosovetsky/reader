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
    account_id: Optional[int] = Field(default=None, foreign_key="account.id", index=True)
    work_id: Optional[int] = Field(default=None, foreign_key="work.id", index=True)
    source_url: str = ""
    last_seen_chapters: int = 0
    has_update: bool = False
    last_checked: Optional[datetime] = None


class SyncState(SQLModel, table=True):
    """Произвольные ключ-значение для состояния sync (этап 3)."""

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
