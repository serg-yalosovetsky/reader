"""Базовый интерфейс загрузчиков фанфиков и общий результат."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DownloadResult:
    """Результат скачивания: готовый файл книги + метаданные."""

    file_path: Path
    file_format: str  # epub | fb2
    title: str = ""
    author: str = ""
    site: str = ""
    source_url: str = ""
    num_chapters: int = 0
    cover_path: Path | None = None
    extra: dict = field(default_factory=dict)


class DownloaderError(Exception):
    """Ошибка скачивания (нераспознанный сайт, сбой сети, требуется логин и т.п.)."""


class UnsupportedURL(DownloaderError):
    """Загрузчик не умеет этот URL — можно попробовать следующий в цепочке."""


class PaidContentError(DownloaderError):
    """Книга платная/неполная на этом источнике — нужен фоллбэк на бесплатный.
    Несёт title/author для поиска на других сайтах."""

    def __init__(self, title: str = "", author: str = "", message: str = ""):
        super().__init__(message or f"Платный контент: {title}")
        self.title = title
        self.author = author
