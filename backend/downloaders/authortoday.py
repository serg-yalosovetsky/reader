"""Адаптер author.today (у FanFicFare его нет).

Портирована логика юзерскрипта AuthorTodayExtractor (Ox90, MIT):
- список глав и метаданные читаются со страниц /work/<id> и /reader/<id>;
- текст каждой главы берётся с /reader/<id>/chapter?id=<cid> (тело data.text
  зашифровано), ключ приходит в заголовке `reader-secret`;
- расшифровка: key = reverse(secret) + "@_@" + userId; XOR посимвольно.
  Для анонимного доступа userId = "" (в JS `app.userId || ""`).

Работает для книг со свободным доступом. Платное/18+ потребует cookies сессии
(этап 4 — аккаунты).
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from ebooklib import epub

from .base import DownloaderError, DownloadResult, PaidContentError, UnsupportedURL

_BASE = "https://author.today"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_ID_RE = re.compile(r"/(?:work|reader)/(\d+)")
_CHAPTERS_RE = re.compile(r"\bchapters\s*:\s*(\[.+?\])\s*,?[\n\r]", re.S)
_USERID_RE = re.compile(r"\buserId\s*:\s*(\d+)")


def _get(c: httpx.Client, url: str, *, attempts: int = 4, **kw) -> httpx.Response:
    """GET с ретраями на транзиентные сетевые/TLS-сбои (author.today флакит)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return c.get(url, **kw)
        except httpx.HTTPError as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise DownloaderError(f"author.today: сетевая ошибка на {url}: {last}")


def _work_id(url: str) -> str:
    m = _ID_RE.search(url)
    if not m:
        raise UnsupportedURL(f"Не похоже на ссылку author.today: {url}")
    return m.group(1)


def _decrypt(text: str, secret: str, user_id: str = "") -> str:
    """XOR-расшифровка текста главы (порт decryptText из юзерскрипта)."""
    key = secret[::-1] + "@_@" + (user_id or "")
    klen = len(key)
    return "".join(
        chr(ord(text[i]) ^ ord(key[i % klen])) for i in range(len(text))
    )



def search_work(title: str, author: str = "") -> str | None:
    """Найти работу на author.today по названию (+ автор для уточнения).
    Возвращает URL первого подходящего результата или None."""
    q = f"{title} {author}".strip() if author else title
    with httpx.Client(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        try:
            r = c.get("https://author.today/search", params={"q": q, "type": "works"})
            if r.status_code != 200:
                return None
            work_ids = list(dict.fromkeys(re.findall(r'href="/work/(\d+)"', r.text)))
            if not work_ids:
                return None
            # Берём первый результат — поиск по точному названию обычно точный
            return f"https://author.today/work/{work_ids[0]}"
        except Exception:
            return None


def count_chapters(url: str) -> int | None:
    """Быстро получить число глав без скачивания текста."""
    import json as _json
    work_id = _work_id(url)
    with httpx.Client(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        try:
            rr = c.get(f"https://author.today/reader/{work_id}")
            m = _CHAPTERS_RE.search(rr.text)
            if not m:
                return None
            arr = _json.loads(m.group(1))
            return len([ch for ch in arr if ch.get("id")])
        except Exception:
            return None

def _login(c: httpx.Client, email: str, password: str) -> bool:
    """Войти в author.today через JSON API. Возвращает True если успешно."""
    import re as _re
    try:
        r = _get(c, f"{_BASE}/account/login")
        form_start = r.text.find('id="loginForm"')
        block = r.text[form_start:form_start + 1000] if form_start >= 0 else r.text
        token_m = _re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', block)
        if not token_m:
            return False
        token = token_m.group(1)
        resp = c.post(
            f"{_BASE}/account/login",
            json={"Login": email, "Password": password},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "RequestVerificationToken": token,
                "Content-Type": "application/json",
            },
            follow_redirects=True,
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return bool(data.get("isSuccessful"))
    except Exception:
        return False


def download(url: str, creds: tuple[str, str] | None = None) -> DownloadResult:
    work_id = _work_id(url)
    with httpx.Client(
        timeout=60, follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        if creds:
            _login(c, creds[0], creds[1])
        # 1) Страница книги — метаданные.
        wr = _get(c, f"{_BASE}/work/{work_id}")
        if wr.status_code != 200:
            raise DownloaderError(f"author.today: страница книги вернула {wr.status_code}")
        title, author, annotation = _parse_work_meta(wr.text)

        # Без входа — платная/18+ недоступна; если залогинились, пробуем скачать.
        if not _is_free(wr.text) and not creds:
            raise PaidContentError(title=title, author=author)

        # 2) Страница ридера — список глав (+ cookies сессии для запросов глав).
        rr = _get(c, f"{_BASE}/reader/{work_id}")
        if rr.status_code != 200:
            raise DownloaderError(f"author.today: ридер вернул {rr.status_code}")
        chapters = _parse_chapters(rr.text)
        if not chapters:
            raise DownloaderError("author.today: не найден список глав (возможно, нужен вход в аккаунт)")
        uid_m = _USERID_RE.search(rr.text)
        user_id = ""  # аноним: в JS `app.userId || ""`, userId:0 тоже даёт ""
        if uid_m and uid_m.group(1) != "0":
            user_id = uid_m.group(1)

        # 3) Текст каждой главы (с паузой — author.today мягко троттлит).
        chapter_htmls: list[tuple[str, str]] = []
        for idx, ch in enumerate(chapters):
            html = _fetch_chapter(c, work_id, str(ch["id"]), user_id)
            chapter_htmls.append((ch.get("title") or "", html))
            if idx + 1 < len(chapters):
                time.sleep(0.25)

    # 4) Сборка EPUB (со встроенной обложкой).
    cover = None
    try:
        from ..app import covers
        cover = covers.fetch_cover_bytes(f"{_BASE}/work/{work_id}")
    except Exception:  # noqa: BLE001
        cover = None
    out = _build_epub(work_id, title, author, annotation, chapter_htmls, cover=cover)
    return DownloadResult(
        file_path=out,
        file_format="epub",
        title=title,
        author=author,
        site="authortoday",
        source_url=url,
        num_chapters=len(chapter_htmls),
        extra={"workdir": str(out.parent)},
    )


def _fetch_chapter(c: httpx.Client, work_id: str, chapter_id: str, user_id: str) -> str:
    url = f"{_BASE}/reader/{work_id}/chapter"
    r = _get(
        c, url,
        params={"id": chapter_id, "_": int(time.time() * 1000)},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{_BASE}/reader/{work_id}",
        },
    )
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise DownloaderError("author.today: неожиданный ответ при загрузке главы") from e
    if not data.get("isSuccessful"):
        msgs = data.get("messages") or []
        if msgs and str(msgs[0]).lower() == "unadulted":
            raise DownloaderError("author.today: контент 18+, требуется подтверждение возраста/вход")
        raise DownloaderError(f"author.today: сервер ответил Unsuccessful для главы {chapter_id}")
    secret = r.headers.get("reader-secret")
    if not secret:
        raise DownloaderError("author.today: не получен ключ reader-secret")
    text = data.get("data", {}).get("text", "") or ""
    if not text:
        # Пустой текст при isSuccessful — обычно мягкий троттлинг; повторим.
        for _ in range(3):
            time.sleep(1.5)
            rr = _get(
                c, url,
                params={"id": chapter_id, "_": int(time.time() * 1000)},
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{_BASE}/reader/{work_id}",
                },
            )
            try:
                d2 = rr.json()
            except json.JSONDecodeError:
                continue
            text = d2.get("data", {}).get("text", "") or ""
            secret = rr.headers.get("reader-secret") or secret
            if text:
                break
    return _decrypt(text, secret, user_id)


def _is_free(html: str) -> bool:
    """Книга бесплатна, если на странице есть «Свободный доступ» и нет покупки."""
    if "Свободный доступ" in html:
        return True
    # Признаки платной: ценник/кнопка покупки.
    paid_markers = ("icon-2-cart", "Купить", "add-to-cart", 'class="price"', "руб.")
    return not any(m in html for m in paid_markers)


def _parse_work_meta(html: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1.book-title") or soup.select_one("[itemprop='name']")
    title = title_el.get_text(strip=True) if title_el else "Без названия"
    authors = soup.select("div.book-authors [itemprop='author'] a") or soup.select("div.book-authors a")
    author = ", ".join(a.get_text(strip=True) for a in authors) if authors else ""
    ann_el = soup.select_one("div.annotation div.rich-content") or soup.select_one("div.annotation")
    annotation = ann_el.get_text("\n", strip=True) if ann_el else ""
    return title, author, annotation


def _parse_chapters(reader_html: str) -> list[dict]:
    m = _CHAPTERS_RE.search(reader_html)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return [{"id": ch.get("id"), "title": ch.get("title", "")} for ch in arr if ch.get("id")]


def _build_epub(
    work_id: str, title: str, author: str, annotation: str,
    chapters: list[tuple[str, str]], cover: bytes | None = None,
) -> Path:
    book = epub.EpubBook()
    book.set_identifier(f"authortoday_{work_id}")
    book.set_title(title)
    book.set_language("ru")
    if author:
        book.add_author(author)
    if annotation:
        book.add_metadata("DC", "description", annotation)
    if cover:
        ext = "png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
        try:
            book.set_cover(f"cover.{ext}", cover)
        except Exception:  # noqa: BLE001
            pass

    spine: list = []
    toc: list = []
    for i, (ch_title, ch_html) in enumerate(chapters, 1):
        name = ch_title or f"Глава {i}"
        item = epub.EpubHtml(title=name, file_name=f"chap_{i}.xhtml", lang="ru")
        # Тело всегда непустое: заголовок + текст (или плейсхолдер), иначе
        # ebooklib падает на генерации nav («Document is empty»).
        body = ch_html.strip() or "<p></p>"
        # ВАЖНО: без декларации <?xml?> и без обёртки <html>/<body> — ebooklib
        # оборачивает сам, а наличие <?xml?> в content приводит к пустому файлу.
        item.content = f"<h2>{_esc(name)}</h2>{body}"
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    book.toc = tuple(toc)
    # Только NCX (EPUB2-оглавление): EpubNav в ebooklib падает на генерации
    # page-list, когда есть пустые/служебные документы. foliate и Calibre
    # прекрасно читают NCX.
    book.add_item(epub.EpubNcx())
    book.spine = spine

    out_dir = Path(tempfile.mkdtemp(prefix="at_"))
    out = out_dir / "book.epub"
    epub.write_epub(str(out), book)
    return out


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
