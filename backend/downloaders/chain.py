"""Выбор загрузчика по URL и цепочка фоллбэков.

Порядок:
1. author.today  -> собственный адаптер (этап 2b);
2. известные FanFicFare-домены -> FanFicFare;
3. иначе -> FanFicFare (вдруг знает), при UnsupportedURL -> FicHub.
"""
from __future__ import annotations

from urllib.parse import urlparse

from . import fanficfare_engine as fff
from . import fichub
from .base import DownloaderError, DownloadResult, UnsupportedURL


def is_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def fetch(query: str) -> DownloadResult:
    """Скачать по ссылке. query должен быть URL (поиск по названию — отдельно)."""
    if not is_url(query):
        raise DownloaderError(
            "Пока поддерживается скачивание по ссылке. Вставьте URL фанфика "
            "(ficbook.net, fanfics.me, author.today, AO3, fanfiction.net)."
        )
    url = query.strip()
    host = (urlparse(url).hostname or "").lower()

    # 1) author.today — отдельный адаптер (если доступен).
    if host.endswith("author.today"):
        try:
            from . import authortoday
        except ImportError:
            raise DownloaderError("Адаптер author.today ещё не подключён.")
        return authortoday.download(url)

    # 2) известные FanFicFare-домены.
    if fff.supports(url):
        return fff.download(url)

    # 3) попытка FanFicFare, затем FicHub.
    try:
        return fff.download(url)
    except UnsupportedURL:
        return fichub.download(url)
