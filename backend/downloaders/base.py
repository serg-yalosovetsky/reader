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
    """Текст на этом источнике недоступен — нужен фоллбэк на бесплатные зеркала.
    Несёт title/author для поиска на других сайтах и ПРИЧИНУ недоступности.

    reason:
      "paid"  — текст за деньги (кнопка «Читать фрагмент» / глава Paid);
      "adult" — возрастной гейт 18+ (ответ `unadulted`): книга может быть
                бесплатной, но требует входа в аккаунт.

    Разница не косметическая: «платно» чинится покупкой или зеркалом, а «18+» —
    рабочим входом в author.today. Один текст на обе причины уводил диагностику
    в сторону покупки книг, которые бесплатны (serg/tasks#319).
    """

    def __init__(
        self,
        title: str = "",
        author: str = "",
        message: str = "",
        reason: str = "paid",
    ):
        default = (
            f"Контент 18+, требуется вход: {title}"
            if reason == "adult"
            else f"Платный контент: {title}"
        )
        super().__init__(message or default)
        self.title = title
        self.author = author
        self.reason = reason
