"""Адаптер searchfloor.org (Цокольный этаж).

Поддерживает:
- /book/<id>            — прямое скачивание книги (FB2.zip) ← основной путь;
- /b/<id>              — страница книги (берём id, качаем /book/<id>);
- /boosty/post/<id>    — пост Boosty (текст из #postContent → EPUB).

Плюс поиск по сайту (search_book) — для фоллбэка платных author.today книг.
"""
from __future__ import annotations

import io
import logging
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

_log = logging.getLogger("reader.searchfloor")

_BASE = "https://searchfloor.org"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# Концевая рекламная врезка Цокольного этажа в FB2: <section> с заголовком
# «Nota bene» (промо сайта, VPN, telegram-бот, «наградите автора»). Вырезаем.
_PROMO_RE = re.compile(
    rb"<section>\s*<title>\s*<p>\s*Nota bene\s*</p>\s*</title>.*?</section>",
    re.S | re.I,
)


def _strip_promo(raw: bytes) -> bytes:
    """Убрать промо-врезку «Nota bene…» из FB2. Работаем НА БАЙТАХ: decode всего файла
    в str раздувает память в разы (FB2 тут до 40+ МБ; кириллица → 2–4 байта/символ +
    копия regex + re-encode = пик 200–300 МБ). «Nota bene» — ASCII, bytes-regexp ловит
    врезку без декода. Best-effort: при любой неожиданности возвращаем исходные байты."""
    try:
        return _PROMO_RE.sub(b"", raw)
    except Exception:  # noqa: BLE001
        return raw


def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("searchfloor.org")


# Скачивание книги терпеливое (большой fb2), а МОНИТОРИНГ — нет: его зонды
# идут по ВСЕМ подпискам каждый тик, и одним залипшим хостом 90-секундный
# таймаут растягивает фазу за пределы самого интервала мониторинга:
# 3 последовательных запроса × 90 с × 70 подписок / 5 воркеров ≈ 63 минуты
# при тике в 20 минут.
_PROBE_TIMEOUT = 15


def _client(timeout: int = 90) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"})


def download(url: str) -> DownloadResult:
    path = urlparse(url).path
    m_book = re.search(r"/(?:book|b)/(\d+)", path)
    if m_book:
        return _download_book(m_book.group(1), url)
    if "/post/" in path:
        return _download_boosty(url)
    raise UnsupportedURL(f"searchfloor: неизвестный тип ссылки {url}")


# Книга только для подписчиков сайта: на /b/<id> рядом с кнопкой скачивания
# стоит замок `<a href="/donate" class="... bi-lock" data-bs-title="Доступ по
# подписке">`, а /book/<id> анониму отвечает 401 — не на минуту, а пока книга
# не выйдет из подписки (обычно онгоинги). Замер 24.10.2026 по 15 книгам: замок
# есть ровно у тех, где /book/<id> отдаёт 401, у остальных 200/206.
# Живой случай serg/tasks#1858: «Крушение сурка. Том 3» — пока author.today
# лежал, монитор поверил плашке «Последняя глава» searchfloor (23), скачать
# оттуда не смог (401) и пять тиков штрафовал подписку «глава недоступна?».
_SUBSCRIPTION_RE = re.compile(r'data-bs-title\s*=\s*"Доступ по подписке"')


class SubscriptionRequired(DownloaderError):
    """Книга на searchfloor отдаётся только подписчикам сайта — анонимно не скачать."""


def is_subscription_only(html: str) -> bool:
    """Помечена ли страница книги /b/<id> замком «Доступ по подписке»."""
    return bool(_SUBSCRIPTION_RE.search(html or ""))


def subscription_only(book_id: str) -> bool | None:
    """Только ли для подписчиков книга <book_id>.

    None — страницу получить не удалось: это «не знаем», а не «свободна»;
    вызывающий решает сам и в лог уже ушла причина."""
    try:
        with _client(_PROBE_TIMEOUT) as c:
            r = c.get(f"{_BASE}/b/{book_id}")
    except httpx.HTTPError as e:
        _log.warning("searchfloor: не удалось открыть /b/%s для проверки подписки: %s: %s",
                     book_id, type(e).__name__, e)
        return None
    if r.status_code != 200:
        _log.warning("searchfloor: /b/%s вернул %s при проверке подписки", book_id, r.status_code)
        return None
    return is_subscription_only(r.text)


# Коды, которыми searchfloor отвечает на скачивание ВРЕМЕННО. 401 — не протухший
# вход (качаем анонимно, и через минуту та же ссылка отдаёт 200): так сайт
# отвечает, пока пересобирает файл тома (живой случай 12.10 00:08 — «Вечно
# голодный студент 10», last-modified файла 00:21). Без повтора зеркало
# выпадало из выбора самой полной копии на весь тик.
_DL_TRANSIENT = {401, 403, 429, 500, 502, 503, 504}
_DL_RETRY_DELAYS = (5, 20)


def _download_book(book_id: str, src_url: str) -> DownloadResult:
    """Скачать книгу целиком: GET /book/<id> -> FB2.zip -> распаковать .fb2."""
    title, author = _book_meta(book_id)
    with _client() as c:
        for attempt in range(len(_DL_RETRY_DELAYS) + 1):
            r = c.get(f"{_BASE}/book/{book_id}")
            if r.status_code == 200 and r.content:
                break
            # 401 бывает и временным (пересборка файла), и постоянным (книга по
            # подписке). Постоянный не лечится повтором — называем причину сразу.
            if r.status_code == 401 and attempt == 0 and subscription_only(book_id):
                raise SubscriptionRequired(
                    f"searchfloor: книга «{title}» доступна только подписчикам сайта "
                    f"(«Доступ по подписке», /book/{book_id} вернул 401)"
                )
            if r.status_code not in _DL_TRANSIENT or attempt == len(_DL_RETRY_DELAYS):
                raise DownloaderError(f"searchfloor: скачивание книги вернуло {r.status_code}")
            time.sleep(_DL_RETRY_DELAYS[attempt])
        blob = r.content

    out_dir = Path(tempfile.mkdtemp(prefix="sf_"))
    # Ответ — zip с .fb2 внутри (Content-Disposition: *.fb2.zip).
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        fb2_name = next((n for n in zf.namelist() if n.lower().endswith(".fb2")), None)
        if fb2_name:
            out = out_dir / "book.fb2"
            out.write_bytes(_strip_promo(zf.read(fb2_name)))
            fmt = "fb2"
        else:
            out = out_dir / "book.epub"  # на случай, если внутри epub
            out.write_bytes(zf.read(zf.namelist()[0]))
            fmt = "epub"
    except zipfile.BadZipFile:
        # не zip — сохраняем как есть (вдруг чистый fb2)
        out = out_dir / "book.fb2"
        out.write_bytes(_strip_promo(blob))
        fmt = "fb2"

    return DownloadResult(
        file_path=out, file_format=fmt, title=title, author=author,
        site="searchfloor", source_url=f"{_BASE}/b/{book_id}",
        num_chapters=0, extra={"workdir": str(out_dir)},
    )


def _book_meta(book_id: str) -> tuple[str, str]:
    """Заголовок/автор со страницы /b/<id> (title: 'Название / Автор')."""
    try:
        with _client(_PROBE_TIMEOUT) as c:
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
    """Поиск книги по названию → id (для фоллбэка). Возвращает book_id или None.

    Короткий таймаут: вызывается из пер-тикового зонда по всем подпискам."""
    q = quote(title)
    with _client(_PROBE_TIMEOUT) as c:
        r = c.get(f"{_BASE}/search?q={q}")
    ids = re.findall(r"/b/(\d+)", r.text)
    # Доп. фильтрация по автору, если задан и встречается рядом — упрощённо берём первый.
    return ids[0] if ids else None
# --- Мониторинг обновлений ---------------------------------------------------
# Страница книги /b/<id> показывает плашку «Последняя глава» (есть только у
# онгоингов; у завершённых её нет — они и не обновляются). Парсим из неё номер
# главы — получаем числовой счётчик для механики last_seen_chapters монитора.

_ORD_UNITS = {
    "первая": 1, "вторая": 2, "третья": 3, "четвертая": 4, "четвёртая": 4,
    "пятая": 5, "шестая": 6, "седьмая": 7, "восьмая": 8, "девятая": 9,
    "десятая": 10, "одиннадцатая": 11, "двенадцатая": 12, "тринадцатая": 13,
    "четырнадцатая": 14, "пятнадцатая": 15, "шестнадцатая": 16,
    "семнадцатая": 17, "восемнадцатая": 18, "девятнадцатая": 19,
    "двадцатая": 20, "тридцатая": 30, "сороковая": 40, "пятидесятая": 50,
}
_ORD_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}


def _parse_chapter_number(marker: str) -> int | None:
    """Номер главы из текста плашки: «Глава 12», «Глава шестая. …»,
    «Глава двадцать первая». Эпилог/пролог и прочее без номера — None."""
    low = marker.lower()
    m = re.search(r"глав\w*\s*№?\s*(\d+)", low)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:^|\s)(\d+)\s*глав", low)
    if m:
        return int(m.group(1))
    if "глав" not in low:
        return None
    words = re.findall(r"[а-яё]+", low)
    for i, w in enumerate(words):
        if w in _ORD_TENS and i + 1 < len(words) and words[i + 1] in _ORD_UNITS:
            unit = _ORD_UNITS[words[i + 1]]
            if unit < 10:
                return _ORD_TENS[w] + unit
        if w in _ORD_UNITS:
            return _ORD_UNITS[w]
    return None


def count_chapters(url: str) -> int | None:
    """Текущее число глав книги по плашке «Последняя глава» на /b/<id>.
    None — плашки нет (завершённая книга) или номер не распарсился."""
    m = re.search(r"/(?:book|b)/(\d+)", urlparse(url).path)
    if not m:
        return None
    try:
        with _client(_PROBE_TIMEOUT) as c:
            r = c.get(f"{_BASE}/b/{m.group(1)}")
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        el = soup.select_one('p[data-bs-title="Последняя глава"]')
        if not el:
            return None
        return _parse_chapter_number(el.get_text(" ", strip=True))
    except httpx.HTTPError:
        return None
