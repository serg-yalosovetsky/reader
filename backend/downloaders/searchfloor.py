"""Адаптер searchfloor.org (агрегатор постов Boosty): /boosty/post/<id>.

Текст поста — в `div#postContent`, заголовок — h1, автор/блог — из <title>
вида "Заголовок / Автор (id)". Одностраничный пост → одна секция EPUB.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .base import DownloaderError, DownloadResult, UnsupportedURL
from .epub_build import build_epub

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_ID_RE = re.compile(r"/post/(\d+)")


def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("searchfloor.org")


def download(url: str) -> DownloadResult:
    m = _ID_RE.search(url)
    if not m:
        raise UnsupportedURL(f"searchfloor: не найден id поста в {url}")
    post_id = m.group(1)

    try:
        with httpx.Client(timeout=40, follow_redirects=True,
                          headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
            for i in range(4):
                try:
                    r = c.get(url)
                    break
                except httpx.HTTPError:
                    time.sleep(0.6 * (i + 1))
            else:
                raise DownloaderError(f"searchfloor: сетевая ошибка на {url}")
    except httpx.HTTPError as e:
        raise DownloaderError(f"searchfloor: {e}") from e

    if r.status_code != 200:
        raise DownloaderError(f"searchfloor: вернул {r.status_code}")

    soup = BeautifulSoup(r.text, "lxml")
    box = soup.select_one("#postContent")
    if not box or not box.get_text(strip=True):
        raise DownloaderError("searchfloor: не найден текст поста (#postContent)")
    for bad in box.find_all(["script", "style", "iframe"]):
        bad.decompose()

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else f"Boosty post {post_id}"
    author = _author_from_title(soup)

    ps = box.find_all("p")
    body = "".join(str(p) for p in ps) if ps else box.decode_contents()

    out = build_epub(f"searchfloor_{post_id}", title, author, [(None, body)])
    return DownloadResult(
        file_path=out, file_format="epub", title=title, author=author,
        site="searchfloor", source_url=url, num_chapters=1,
        extra={"workdir": str(out.parent)},
    )


def _author_from_title(soup: BeautifulSoup) -> str:
    """<title> = 'Заголовок / Автор (id)' → 'Автор'."""
    raw = soup.title.get_text(strip=True) if soup.title else ""
    m = re.search(r"/\s*([^/(]+?)\s*\(\d+\)\s*$", raw)
    return m.group(1).strip() if m else ""
