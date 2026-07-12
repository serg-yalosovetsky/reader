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
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TITLE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s+(.*?)\s*[|/]", re.S)


def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("readli.net")


def _book_id(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    bid = qs.get("b", [None])[0]
    if bid:
        return bid
    # slug-страница книги (/vechno-golodnyiy-student-6/) → найти ссылку на читалку.
    with httpx.Client(
        timeout=40, follow_redirects=True, headers={"User-Agent": _UA}
    ) as c:
        html = _get(c, url).text
    m = re.search(r"/chitat-online/\?b=(\d+)", html)
    if not m:
        raise UnsupportedURL(f"readli: не найден b и ссылка на читалку в {url}")
    return m.group(1)


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def _sim(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _author_ok(want: str, cand: str) -> bool:
    """Автор совпал, если похож ИЛИ псевдоним вложен в полное имя (и наоборот).
    readli часто пишет «Абрамов Владимир "noslnosl"», а author.today — «noslnosl»."""
    wn, cn = _norm(want), _norm(cand)
    if not wn or not cn:
        return True  # нечего сравнивать — не отбраковываем по автору
    if wn in cn or cn in wn:
        return True
    return _sim(want, cand) >= _AUTHOR_MIN


# Пороги соответствия при поиске. Название — основной сигнал; автор уточняет.
# Без этой проверки readli-поиск (нередко отдаёт нерелевантный список) утаскивал
# в фоллбэк ЧУЖУЮ книгу, а _search_free берёт самую объёмную — и полный чужой
# роман вытеснял правильную книгу. Поэтому: нет уверенного совпадения → None.
_TITLE_MIN = 0.60
_AUTHOR_MIN = 0.40


def search_and_download(title: str, author: str = ""):
    """Поиск книги по названию+автору на readli → скачать (best-effort фоллбэк).

    Возвращает None, если ни один результат не совпал с запросом по названию
    (и автору, если он известен) — лучше ничего, чем скачать не ту книгу."""
    from urllib.parse import quote

    # ВАЖНО: поиск readli — эндпоинт /srch/?q= (не /?s=, который отдаёт дефолтный
    # список независимо от запроса). Карточки: article.book (стр. /srch/) либо
    # div.book__all (стр. /?s=).
    with httpx.Client(
        timeout=40,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru"},
    ) as c:
        html = _get(c, f"{_BASE}/srch/?q={quote(title)}").text
    soup = BeautifulSoup(html, "lxml")

    best_href, best_score = None, -1.0
    for card in soup.select("article.book, div.book__all")[:15]:
        link = card.select_one("h4.book__title a, .book__title a, a.book__link")
        if not link or not link.get("href"):
            continue
        cand_title = link.get("title") or link.get_text(strip=True)
        title_s = _sim(title, cand_title)
        if title_s < _TITLE_MIN:
            continue
        if author:
            au = card.select_one(
                ".book__authors a[href*='/avtor/'], a[href*='/avtor/']"
            )
            cand_author = au.get_text(strip=True) if au else ""
            if cand_author and not _author_ok(author, cand_author):
                continue
        # название — основной ранжирующий сигнал (точный «Том 1» бьёт «Том 2»)
        if title_s > best_score:
            best_score, best_href = title_s, link["href"]

    if not best_href:
        return None
    return download(best_href if best_href.startswith("http") else _BASE + best_href)


def _get(c: httpx.Client, url: str, attempts: int = 4) -> httpx.Response:
    last = None
    for i in range(attempts):
        try:
            return c.get(url)
        except httpx.HTTPError as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise DownloaderError(f"readli: сетевая ошибка на {url}: {last}")


def count_chapters(url: str) -> int | None:
    """«Главы» readli = число страниц читалки (пагинация). Растёт при дописывании
    книги — это и есть сигнал обновления (мониторится как last_seen_chapters).
    Берём ТОЛЬКО 1-ю страницу: total зашит в <title> (или max pg= в ссылках),
    качать всю книгу не нужно. None — не распарсили/сеть."""
    try:
        bid = _book_id(url)
    except (UnsupportedURL, DownloaderError):
        return None
    try:
        with httpx.Client(
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
        ) as c:
            r = _get(c, f"{_BASE}/chitat-online/?b={bid}&pg=1")
        if r.status_code != 200:
            return None
        _title, total = _parse_head(BeautifulSoup(r.text, "lxml"))
        return total or None
    except (httpx.HTTPError, DownloaderError):
        return None


def download(url: str) -> DownloadResult:
    bid = _book_id(url)
    page_url = lambda n: f"{_BASE}/chitat-online/?b={bid}&pg={n}"

    with httpx.Client(
        timeout=40,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
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

    cover = None
    try:
        from ..app import covers

        cover = covers.fetch_cover_bytes(page_url(1))
    except Exception:  # noqa: BLE001
        cover = None
    out = build_epub(f"readli_{bid}", title, author, sections, cover=cover)
    return DownloadResult(
        file_path=out,
        file_format="epub",
        title=title,
        author=author,
        site="readli",
        source_url=f"{_BASE}/chitat-online/?b={bid}",
        num_chapters=len(sections),
        extra={"workdir": str(out.parent)},
    )


def _parse_head(soup: BeautifulSoup) -> tuple[str, int]:
    raw = soup.title.get_text(strip=True) if soup.title else ""
    m = _TITLE_RE.match(raw)
    if m:
        return m.group(3).strip(), int(m.group(2))
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else (raw or "Без названия")
    # запасной способ найти число страниц — максимум pg= в ссылках
    pages = [
        int(mm.group(1))
        for a in soup.find_all("a", href=True)
        if (mm := re.search(r"[?&]pg=(\d+)", a["href"]))
    ]
    return title, (max(pages) if pages else 1)


def _parse_author(soup: BeautifulSoup) -> str:
    a = (
        soup.select_one('a[href*="/avtor/"]')
        or soup.select_one('[itemprop="author"]')
        or soup.select_one(".book__author a")
    )
    return a.get_text(strip=True) if a else ""


def _extract_text(soup: BeautifulSoup) -> str:
    box = soup.select_one("div.reading__text") or soup.select_one(
        "article.reading__content"
    )
    if not box:
        return ""
    for bad in box.find_all(["script", "style", "ins", "iframe"]):
        bad.decompose()
    ps = box.find_all("p")
    if ps:
        return "".join(str(p) for p in ps)
    return box.decode_contents()
