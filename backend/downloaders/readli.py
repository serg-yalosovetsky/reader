"""Адаптер readli.net (онлайн-читалка, постраничная: /chitat-online/?b=<id>&pg=<n>).

Книга разбита на страницы пагинации (не главы). Собираем текст со всех страниц
(`div.reading__text`) и склеиваем в EPUB (одна секция на страницу — лёгкие
документы, foliate грузит инкрементально).
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import DownloaderError, DownloadResult, UnsupportedURL
from .epub_build import build_epub

_BASE = "https://readli.net"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TITLE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s+(.*?)\s*[|/]", re.S)


def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("readli.net")


def _book_id(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    bid = qs.get("b", [None])[0]
    if not bid:
        raise UnsupportedURL(f"readli: не найден параметр b в {url}")
    return bid


def _get(c: httpx.Client, url: str, attempts: int = 4) -> httpx.Response:
    last = None
    for i in range(attempts):
        try:
            return c.get(url)
        except httpx.HTTPError as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise DownloaderError(f"readli: сетевая ошибка на {url}: {last}")


def download(url: str) -> DownloadResult:
    bid = _book_id(url)
    page_url = lambda n: f"{_BASE}/chitat-online/?b={bid}&pg={n}"

    with httpx.Client(timeout=40, follow_redirects=True,
                      headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"}) as c:
        first = _get(c, page_url(1))
        if first.status_code != 200:
            raise DownloaderError(f"readli: страница вернула {first.status_code}")
        soup = BeautifulSoup(first.text, "lxml")
        title, total = _parse_head(soup)
        author = _parse_author(soup)

        sections: list[tuple[str | None, str]] = []
        sections.append((None, _extract_text(soup)))
        for n in range(2, total + 1):
            s = BeautifulSoup(_get(c, page_url(n)).text, "lxml")
            sections.append((None, _extract_text(s)))
            time.sleep(0.2)

    if not any(html.strip() for _, html in sections):
        raise DownloaderError("readli: не удалось извлечь текст книги")

    out = build_epub(f"readli_{bid}", title, author, sections)
    return DownloadResult(
        file_path=out, file_format="epub", title=title, author=author,
        site="readli", source_url=f"{_BASE}/chitat-online/?b={bid}",
        num_chapters=len(sections), extra={"workdir": str(out.parent)},
    )


def _parse_head(soup: BeautifulSoup) -> tuple[str, int]:
    raw = soup.title.get_text(strip=True) if soup.title else ""
    m = _TITLE_RE.match(raw)
    if m:
        return m.group(3).strip(), int(m.group(2))
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else (raw or "Без названия")
    # запасной способ найти число страниц — максимум pg= в ссылках
    pages = [int(mm.group(1)) for a in soup.find_all("a", href=True)
             if (mm := re.search(r"[?&]pg=(\d+)", a["href"]))]
    return title, (max(pages) if pages else 1)


def _parse_author(soup: BeautifulSoup) -> str:
    a = (soup.select_one('a[href*="/avtor/"]') or soup.select_one('[itemprop="author"]')
         or soup.select_one('.book__author a'))
    return a.get_text(strip=True) if a else ""


def _extract_text(soup: BeautifulSoup) -> str:
    box = soup.select_one("div.reading__text") or soup.select_one("article.reading__content")
    if not box:
        return ""
    for bad in box.find_all(["script", "style", "ins", "iframe"]):
        bad.decompose()
    ps = box.find_all("p")
    if ps:
        return "".join(str(p) for p in ps)
    return box.decode_contents()
