"""Адаптер searchfloor.org (Цокольный этаж).

Поддерживает:
- /book/<id>            — прямое скачивание книги (FB2.zip) ← основной путь;
- /b/<id>              — страница книги (берём id, качаем /book/<id>);
- /boosty/post/<id>    — пост Boosty (текст из #postContent → EPUB).

Плюс поиск по сайту (search_book) — для фоллбэка платных author.today книг.
"""
from __future__ import annotations

import io
import re
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import DownloaderError, DownloadResult, UnsupportedURL
from .epub_build import build_epub

_BASE = "https://searchfloor.org"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("searchfloor.org")


def _client() -> httpx.Client:
    return httpx.Client(timeout=90, follow_redirects=True,
                        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"})


def download(url: str) -> DownloadResult:
    path = urlparse(url).path
    m_book = re.search(r"/(?:book|b)/(\d+)", path)
    if m_book:
        return _download_book(m_book.group(1), url)
    if "/post/" in path:
        return _download_boosty(url)
    raise UnsupportedURL(f"searchfloor: неизвестный тип ссылки {url}")


def _download_book(book_id: str, src_url: str) -> DownloadResult:
    """Скачать книгу целиком: GET /book/<id> -> FB2.zip -> распаковать .fb2."""
    title, author = _book_meta(book_id)
    with _client() as c:
        r = c.get(f"{_BASE}/book/{book_id}")
        if r.status_code != 200 or not r.content:
            raise DownloaderError(f"searchfloor: скачивание книги вернуло {r.status_code}")
        blob = r.content

    out_dir = Path(tempfile.mkdtemp(prefix="sf_"))
    # Ответ — zip с .fb2 внутри (Content-Disposition: *.fb2.zip).
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        fb2_name = next((n for n in zf.namelist() if n.lower().endswith(".fb2")), None)
        if fb2_name:
            out = out_dir / "book.fb2"
            out.write_bytes(zf.read(fb2_name))
            fmt = "fb2"
        else:
            out = out_dir / "book.epub"  # на случай, если внутри epub
            out.write_bytes(zf.read(zf.namelist()[0]))
            fmt = "epub"
    except zipfile.BadZipFile:
        # не zip — сохраняем как есть (вдруг чистый fb2)
        out = out_dir / "book.fb2"
        out.write_bytes(blob)
        fmt = "fb2"

    return DownloadResult(
        file_path=out, file_format=fmt, title=title, author=author,
        site="searchfloor", source_url=f"{_BASE}/b/{book_id}",
        num_chapters=0, extra={"workdir": str(out_dir)},
    )


def _book_meta(book_id: str) -> tuple[str, str]:
    """Заголовок/автор со страницы /b/<id> (title: 'Название / Автор')."""
    try:
        with _client() as c:
            r = c.get(f"{_BASE}/b/{book_id}")
        soup = BeautifulSoup(r.text, "lxml")
        h1s = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2"])]
        raw = soup.title.get_text(strip=True) if soup.title else ""
        # title вида "Название / Автор"
        parts = [p.strip() for p in raw.split("/")]
        title = parts[0] if parts else (h1s[0] if h1s else f"Книга {book_id}")
        author = parts[1] if len(parts) > 1 else ""
        a = soup.select_one('a[href^="/a/"]')
        if a:
            author = a.get_text(strip=True) or author
        return title, author
    except (httpx.HTTPError, Exception):  # noqa: BLE001
        return f"Книга {book_id}", ""


def _download_boosty(url: str) -> DownloadResult:
    post_id = re.search(r"/post/(\d+)", url)
    post_id = post_id.group(1) if post_id else "0"
    with _client() as c:
        for i in range(4):
            try:
                r = c.get(url); break
            except httpx.HTTPError:
                time.sleep(0.6 * (i + 1))
        else:
            raise DownloaderError(f"searchfloor: сетевая ошибка на {url}")
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
    ps = box.find_all("p")
    body = "".join(str(p) for p in ps) if ps else box.decode_contents()
    cover = None
    try:
        from ..app import covers
        cover = covers.fetch_cover_bytes(url)
    except Exception:  # noqa: BLE001
        cover = None
    out = build_epub(f"searchfloor_{post_id}", title, "", [(None, body)], cover=cover)
    return DownloadResult(file_path=out, file_format="epub", title=title, author="",
                          site="searchfloor", source_url=url, num_chapters=1,
                          extra={"workdir": str(out.parent)})


def search_book(title: str, author: str = "") -> str | None:
    """Поиск книги по названию → id (для фоллбэка). Возвращает book_id или None."""
    q = quote(title)
    with _client() as c:
        r = c.get(f"{_BASE}/search?q={q}")
    ids = re.findall(r"/b/(\d+)", r.text)
    # Доп. фильтрация по автору, если задан и встречается рядом — упрощённо берём первый.
    return ids[0] if ids else None
