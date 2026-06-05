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
from .base import DownloaderError, DownloadResult, PaidContentError, UnsupportedURL


def is_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def fetch(query: str, creds: tuple[str, str] | None = None) -> DownloadResult:
    """Скачать по ссылке. query должен быть URL (поиск по названию — отдельно).
    creds (username, password) пробрасываются в FanFicFare для закрытого/18+."""
    if not is_url(query):
        raise DownloaderError(
            "Пока поддерживается скачивание по ссылке. Вставьте URL фанфика "
            "(ficbook.net, fanfics.me, author.today, AO3, fanfiction.net)."
        )
    url = query.strip()
    host = (urlparse(url).hostname or "").lower()
    opts = {"_creds": creds} if creds else None

    # 1) сайты со своими адаптерами.
    if host.endswith("author.today"):
        from . import authortoday
        try:
            return authortoday.download(url)
        except PaidContentError as e:
            # Платная на AT — ищем полную книгу в бесплатных источниках.
            return _fallback_free(e.title, e.author)
    if host.endswith("readli.net"):
        from . import readli
        return readli.download(url)
    if host.endswith("searchfloor.org"):
        from . import searchfloor
        return searchfloor.download(url)

    # 2) известные FanFicFare-домены.
    if fff.supports(url):
        return fff.download(url, extra_options=opts)

    # 3) попытка FanFicFare, затем FicHub.
    try:
        return fff.download(url, extra_options=opts)
    except UnsupportedURL:
        return fichub.download(url)


def _fallback_free(title: str, author: str) -> DownloadResult:
    """Найти полную книгу по названию в бесплатных источниках (searchfloor → readli)."""
    if title:
        from . import searchfloor
        try:
            bid = searchfloor.search_book(title, author)
            if bid:
                return searchfloor._download_book(bid, f"https://searchfloor.org/b/{bid}")
        except DownloaderError:
            pass
        from . import readli
        try:
            r = readli.search_and_download(title, author)
            if r:
                return r
        except DownloaderError:
            pass
    raise DownloaderError(
        f"Книга платная на author.today, а в бесплатных источниках "
        f"(searchfloor/readli) не найдена: «{title}»."
    )
