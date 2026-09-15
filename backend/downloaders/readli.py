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

from ..app import progress
from .base import DownloaderError, DownloadResult, UnsupportedURL
from .epub_build import build_epub
from .textclean import clean_html, clean_title

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
        best_href, authors = _pick(
            BeautifulSoup(html, "lxml").select(_CARD_SEL)[:15], title, author
        )
        # Поиск readli отстаёт от каталога: свежий том есть на странице автора, но
        # не в /srch/ (живой случай 2026-09-15 — «Вечно голодный студент 10»).
        # Идём туда только за автором, совпавшим с запросом: без автора чужая
        # страница ничего не доказывает.
        if not best_href and authors:
            best_href = _from_author_pages(c, sorted(authors), title, author)

    if not best_href:
        return None
    return download(best_href if best_href.startswith("http") else _BASE + best_href)


_CARD_SEL = "article.book, div.book__all"
_LINK_SEL = "h4.book__title a, .book__title a, a.book__link"
_AUTHOR_SEL = ".book__authors a[href*='/avtor/'], a[href*='/avtor/']"
# Страниц автора обходим не больше стольких: у плодовитого автора их десятки, а
# листинг идёт от новых книг к старым — свежий том почти всегда на первых.
_AUTHOR_PAGES_MAX = 8


def _vol(title: str) -> int:
    """Номер тома из названия; без номера — том 1, как в book_identity.title_matches."""
    from ..app.book_identity import _title_key

    return _title_key(title)[1] or 1


def _pick(cards, title: str, author: str) -> tuple[str | None, set[str]]:
    """Карточка под запрос и страницы авторов, прошедших сверку.

    Номер тома — жёсткий фильтр, а не ранжирование: у томов одной серии похожесть
    названий одинакова (0.94), и «первый из равных» на запрос тома 10 отдавал
    том 2. Дальше его отбрасывал same_book, и книга оставалась без зеркала вовсе
    (spec.reader.update-pipeline v11).
    """
    want_vol = _vol(title)
    best_href, best_score = None, -1.0
    authors: set[str] = set()
    for card in cards:
        link = card.select_one(_LINK_SEL)
        if not link or not link.get("href"):
            continue
        cand_title = link.get("title") or link.get_text(strip=True)
        title_s = _sim(title, cand_title)
        if title_s < _TITLE_MIN:
            continue
        if author:
            au = card.select_one(_AUTHOR_SEL)
            cand_author = au.get_text(strip=True) if au else ""
            if cand_author and not _author_ok(author, cand_author):
                continue
            if cand_author and au.get("href"):
                authors.add(au["href"])
        if _vol(cand_title) != want_vol:
            continue
        # название — основной ранжирующий сигнал (точный «Том 1» бьёт «Том 2»)
        if title_s > best_score:
            best_score, best_href = title_s, link["href"]
    return best_href, authors


def _from_author_pages(
    c: httpx.Client, author_hrefs: list[str], title: str, author: str
) -> str | None:
    """Найти нужный том на страницах автора (/avtor/<slug>/page/<n>/)."""
    for href in author_hrefs:
        base = (href if href.startswith("http") else _BASE + href).rstrip("/")
        seen: set[str] = set()
        for n in range(1, _AUTHOR_PAGES_MAX + 1):
            r = _get(c, f"{base}/" if n == 1 else f"{base}/page/{n}/")
            if r.status_code != 200:
                break
            cards = BeautifulSoup(r.text, "lxml").select(_CARD_SEL)
            picked, _ = _pick(cards, title, author)
            if picked:
                return picked
            hrefs = {
                a["href"]
                for a in (k.select_one(_LINK_SEL) for k in cards)
                if a and a.get("href")
            }
            # Страница без новых карточек — листинг кончился (на случай, если за
            # концом readli отдаёт повтор, а не 404).
            if not hrefs - seen:
                break
            seen |= hrefs
    return None


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
        progress.report(1, total, "pages", "скачивание", title=title)

        # Собираем HTML ВСЕХ страниц (с сохранением <h3>Глава N</h3>), затем режем
        # на реальные главы — readli пагинирует книгу, но главы размечены <h3> и
        # тянутся через несколько страниц. Резать по страницам = терять главы.
        pages_html: list[str] = [_page_html(soup)]
        for n in range(2, total + 1):
            pages_html.append(_page_html(BeautifulSoup(_get(c, page_url(n)).text, "lxml")))
            progress.report(n, total, "pages")
            time.sleep(0.2)

    full = "".join(pages_html)
    if not full.strip():
        raise DownloaderError("readli: не удалось извлечь текст книги")
    sections = _split_chapters(full)

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


def _page_html(soup: BeautifulSoup) -> str:
    """Внутренний HTML читалки-страницы С СОХРАНЕНИЕМ структуры (заголовки <h3> глав
    + абзацы), в отличие от _extract_text (только <p>). Чистим скрипты/рекламу."""
    box = soup.select_one("div.reading__text") or soup.select_one(
        "article.reading__content"
    )
    if not box:
        return ""
    for bad in box.find_all(["script", "style", "ins", "iframe"]):
        bad.decompose()
    # Служебные комментарии (<!-- quoter = 1; -->), рекламные блоки и промо-хвост
    # AT: без этого они переживают сборку EPUB и видны читателю как текст.
    return clean_html(box.decode_contents())


def _split_chapters(full_html: str) -> list[tuple[str | None, str]]:
    """Разрезать склеенный HTML книги на реальные главы по заголовкам <h3> (readli
    помечает ими «Глава N»). Возвращает [(заголовок|None, html_главы)]. Нет заголовков
    — одна секция (fallback, книга не пустая)."""
    frag = BeautifulSoup(full_html, "html.parser")
    chapters: list[tuple[str | None, list[str]]] = []
    cur_title: str | None = None
    cur_parts: list[str] = []
    for el in frag.children:
        name = getattr(el, "name", None)
        if name in ("h1", "h2", "h3", "h4"):
            if cur_parts or cur_title:
                chapters.append((cur_title, cur_parts))
            # Источник склеивает номер и название («Глава перваяЭскадрон»).
            cur_title = clean_title(el.get_text(" ", strip=True)) or None
            cur_parts = []
        else:
            cur_parts.append(str(el))
    if cur_parts or cur_title:
        chapters.append((cur_title, cur_parts))
    out = [(ttl, "".join(parts).strip()) for ttl, parts in chapters]
    out = [(ttl, html) for ttl, html in out if html or ttl]
    return out or [(None, full_html)]


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
