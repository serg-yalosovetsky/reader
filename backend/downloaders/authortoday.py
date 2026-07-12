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


class _PaidChapter(DownloaderError):
    """Внутренний сигнал: конкретная глава платная/недоступна анонимно.
    Ловится в download() для сборки бесплатной части + фоллбэка на зеркала."""


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
    return "".join(chr(ord(text[i]) ^ ord(key[i % klen])) for i in range(len(text)))


def search_work(title: str, author: str = "") -> str | None:
    """Найти работу на author.today по названию + автору.
    Требует совпадения названия (>=0.80) и, если автор известен, автора (>=0.35).
    Это предотвращает скачивание книг с похожим названием, но другим автором."""
    from difflib import SequenceMatcher

    def _sim(a: str, b: str) -> float:
        a_n = re.sub(r"\W+", " ", a.lower()).strip()
        b_n = re.sub(r"\W+", " ", b.lower()).strip()
        return SequenceMatcher(None, a_n, b_n).ratio()

    q = f"{title} {author}".strip() if author else title
    with httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        try:
            r = c.get("https://author.today/search", params={"q": q, "type": "works"})
            if r.status_code != 200:
                return None
            # Кандидатов собираем из ДВУХ типов блоков выдачи AT, т.к. нужная работа
            # непредсказуемо попадает то в один, то в другой:
            #   • .book-title (+ .book-author) — основные результаты;
            #   • .bookcard-title (+ .bookcard-authors) — боковые карточки
            #     (частичные совпадения по подсвеченным <em>-словам).
            # Раньше парсили только один тип — и промахивались мимо реальных фиков
            # («Сломанный Меч» был в book-title, «Бродяга Грег» — в bookcard-title).
            title_by_id: dict[str, str] = {}
            author_by_id: dict[str, str] = {}
            work_ids: list[str] = []

            def _clean(s: str) -> str:
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()

            def _collect(title_pat: str, author_pat: str) -> None:
                # Название — обязательно; автор — опционально (сопоставляем по
                # порядку появления в пределах одного типа блока). Требовать блок
                # автора в одном regex с названием нельзя: у части карточек его нет
                # в ожидаемом виде, и тогда терялась вся карточка (так «Бродяга
                # Грег» выпадал, хотя присутствовал в bookcard-title).
                ids = re.findall(title_pat, r.text, re.S)
                authors = re.findall(author_pat, r.text, re.S)
                for i, (wid, t_raw) in enumerate(ids):
                    t = _clean(t_raw)
                    if not t or wid in title_by_id:
                        continue
                    title_by_id[wid] = t
                    if i < len(authors):
                        a = _clean(authors[i])
                        if a:
                            author_by_id[wid] = a
                    work_ids.append(wid)

            _collect(
                r'<div class="book-title">\s*<a[^>]*href="/work/(\d+)"[^>]*>(.*?)</a>',
                r'<div class="book-author">\s*<a[^>]*>([^<]+)</a>',
            )
            _collect(
                r'<div class="bookcard-title">.*?href="/work/(\d+)"[^>]*>(.*?)</a>',
                r'<div class="bookcard-authors">.*?<a[^>]*>([^<]+)</a>',
            )
            if not work_ids:
                return None

            TITLE_MIN = 0.80
            AUTHOR_MIN = 0.35

            best_id, best_score = None, -1.0
            # Окно кандидатов: не только топ-5 выдачи. AT сортирует по популярности,
            # и точное совпадение по названию бывает не в первой пятёрке (напр.
            # «Сломанный Меч» ниже более раскрученных «Меч…»). Фильтр по
            # similarity отсекает нерелевантное, поэтому окно можно держать широким.
            for wid in work_ids[:30]:
                t = title_by_id.get(wid)
                if not t:
                    continue
                title_s = _sim(title, t)
                if title_s < TITLE_MIN:
                    continue
                score = title_s
                if author:
                    at_author = author_by_id.get(wid, "")
                    if at_author:
                        author_s = _sim(author, at_author)
                        # Разные авторы отсекаем, только если название НЕ почти
                        # точное — иначе рискуем выкинуть верный фик из-за сбитого
                        # по порядку сопоставления автора (блоки без автора).
                        if author_s < AUTHOR_MIN and title_s < 0.95:
                            continue
                        score = (title_s + author_s) / 2
                if score > best_score:
                    best_score = score
                    best_id = wid

            if best_id is None:
                return None
            return f"https://author.today/work/{best_id}"
        except Exception:
            return None


def count_chapters(url: str) -> int | None:
    """Быстро получить число глав без скачивания текста."""
    work_id = _work_id(url)
    with httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        try:
            rr = c.get(f"https://author.today/reader/{work_id}")
            m = _CHAPTERS_RE.search(rr.text)
            if not m:
                return None
            arr = json.loads(m.group(1))
            return len([ch for ch in arr if ch.get("id")])
        except Exception:
            return None


def fetch_meta(url: str) -> dict | None:
    """Лёгкие метаданные книги AT (title/author/annotation/series[_index]) для
    сверки идентичности через book_identity.same_book. Без скачивания глав."""
    work_id = _work_id(url)
    with httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        try:
            r = c.get(f"{_BASE}/work/{work_id}")
            if r.status_code != 200:
                return None
            title, author, annotation, extra = _parse_work_meta(r.text)
        except Exception:  # noqa: BLE001
            return None
    meta = {"title": title, "author": author, "annotation": annotation}
    if extra.get("series"):
        meta["series"] = extra["series"]
    if extra.get("series_index"):
        meta["series_index"] = extra["series_index"]
    return meta


def _login(c: httpx.Client, email: str, password: str) -> bool:
    """Войти в author.today через JSON API. Возвращает True если успешно."""
    try:
        r = _get(c, f"{_BASE}/account/login")
        form_start = r.text.find('id="loginForm"')
        block = r.text[form_start : form_start + 1000] if form_start >= 0 else r.text
        token_m = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', block
        )
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
        data = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        return bool(data.get("isSuccessful"))
    except Exception:
        return False


def download(url: str, creds: tuple[str, str] | None = None) -> DownloadResult:
    work_id = _work_id(url)
    with httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": _UA, "Accept-Language": "ru,en;q=0.8"},
    ) as c:
        if creds:
            _login(c, creds[0], creds[1])
        # 1) Страница книги — метаданные.
        wr = _get(c, f"{_BASE}/work/{work_id}")
        if wr.status_code != 200:
            raise DownloaderError(
                f"author.today: страница книги вернула {wr.status_code}"
            )
        title, author, annotation, at_meta = _parse_work_meta(wr.text)

        # Без входа — платная/18+ недоступна; если залогинились, пробуем скачать.
        if not _is_free(wr.text) and not creds:
            raise PaidContentError(title=title, author=author)

        # 2) Страница ридера — список глав (+ cookies сессии для запросов глав).
        rr = _get(c, f"{_BASE}/reader/{work_id}")
        if rr.status_code != 200:
            raise DownloaderError(f"author.today: ридер вернул {rr.status_code}")
        chapters = _parse_chapters(rr.text)
        if not chapters:
            raise DownloaderError(
                "author.today: не найден список глав (возможно, нужен вход в аккаунт)"
            )
        uid_m = _USERID_RE.search(rr.text)
        user_id = ""  # аноним: в JS `app.userId || ""`, userId:0 тоже даёт ""
        if uid_m and uid_m.group(1) != "0":
            user_id = uid_m.group(1)

        # 3) Текст каждой главы (с паузой — author.today мягко троттлит).
        chapter_htmls: list[tuple[str, str]] = []
        paid_tail = False  # встретилась платная глава (частично-платная книга)
        try:
            for idx, ch in enumerate(chapters):
                try:
                    html = _fetch_chapter(c, work_id, str(ch["id"]), user_id)
                except _PaidChapter:
                    # Хвост книги платный (главы публикуются по порядку — дальше
                    # тоже платные). Останавливаемся: собрали бесплатную часть.
                    paid_tail = True
                    break
                chapter_htmls.append((ch.get("title") or "", html))
                if idx + 1 < len(chapters):
                    time.sleep(0.25)
        except DownloaderError as e:
            # 18+/возрастной гейт без входа -> отдаём как PaidContent, чтобы chain
            # нашёл полный текст в бесплатных зеркалах (searchfloor/readli).
            msg = str(e).lower()
            if "18+" in msg or "unadulted" in msg or "возраст" in msg:
                raise PaidContentError(title=title, author=author) from e
            raise

        # Ни одной бесплатной главы — книга целиком платная. Отдаём как
        # PaidContent, чтобы chain искал полный текст в бесплатных зеркалах.
        if not chapter_htmls:
            raise PaidContentError(title=title, author=author)

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
        extra={
            "workdir": str(out.parent),
            # частично-платная: собрано меньше глав, чем есть на AT — chain
            # попробует найти более полную бесплатную версию на зеркалах.
            "partial_paid": paid_tail,
            "total_chapters": len(chapters),
            **at_meta,
        },
    )


def _fetch_chapter(c: httpx.Client, work_id: str, chapter_id: str, user_id: str) -> str:
    url = f"{_BASE}/reader/{work_id}/chapter"
    r = _get(
        c,
        url,
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
        raise DownloaderError(
            "author.today: неожиданный ответ при загрузке главы"
        ) from e
    if not data.get("isSuccessful"):
        msgs = data.get("messages") or []
        first = str(msgs[0]).lower() if msgs else ""
        if first == "unadulted":
            raise DownloaderError(
                "author.today: контент 18+, требуется подтверждение возраста/вход"
            )
        if first == "unauthorized":
            # Глава платная/недоступна анонимно (частично-платная книга: пролог
            # бесплатно, хвост за деньги). Помечаем, чтобы download() собрал
            # бесплатную часть и ушёл в фоллбэк на бесплатные зеркала.
            raise _PaidChapter(
                f"author.today: глава {chapter_id} недоступна (Unauthorized)"
            )
        raise DownloaderError(
            f"author.today: сервер ответил Unsuccessful для главы {chapter_id}"
        )
    secret = r.headers.get("reader-secret")
    if not secret:
        raise DownloaderError("author.today: не получен ключ reader-secret")
    text = data.get("data", {}).get("text", "") or ""
    if not text:
        # Пустой текст при isSuccessful — обычно мягкий троттлинг; повторим.
        for _ in range(3):
            time.sleep(1.5)
            rr = _get(
                c,
                url,
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


def _parse_work_meta(html: str) -> tuple[str, str, str, dict]:
    """Разбор страницы книги author.today.

    Возвращает (title, author, annotation, extra), где extra может содержать
    genres (JSON-массив), rating, status, words — для сохранения в Work."""
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one("h1.book-title") or soup.select_one("[itemprop='name']")
    title = title_el.get_text(strip=True) if title_el else "Без названия"
    authors = soup.select("div.book-authors [itemprop='author'] a") or soup.select(
        "div.book-authors a"
    )
    author = ", ".join(a.get_text(strip=True) for a in authors) if authors else ""
    ann_el = soup.select_one("div.annotation div.rich-content") or soup.select_one(
        "div.annotation"
    )
    annotation = ann_el.get_text("\n", strip=True) if ann_el else ""

    extra: dict = {}
    # Жанры — ссылки на /work/genre/<slug> (кроме навигационных /work/genre/all/…).
    genres: list[str] = []
    for a in soup.select("a[href*='/work/genre/']"):
        href = a.get("href", "")
        if "/work/genre/all/" in href:
            continue
        txt = a.get_text(strip=True)
        if txt and txt not in genres:
            genres.append(txt)
    if genres:
        extra["genres"] = json.dumps(genres, ensure_ascii=False)
    # Цикл/серия — ссылка /work/series/<id>; рядом бывает «#N».
    ser = soup.select_one("a[href*='/work/series/']")
    if ser:
        s_txt = ser.get_text(strip=True)
        if s_txt:
            extra["series"] = s_txt
        around = ser.parent.get_text(" ", strip=True) if ser.parent else s_txt
        mnum = re.search(r"#\s*(\d+)", around)
        if mnum:
            extra["series_index"] = int(mnum.group(1))
    # Возрастной рейтинг (бейдж 18+/16+…).
    age = soup.select_one(".book-age-limit, .badge-age, [class*='age']")
    if age:
        m = re.search(r"\d+\+", age.get_text(" ", strip=True))
        if m:
            extra["rating"] = m.group(0)
    # Статус.
    status_el = soup.select_one(".book-status, [class*='status']")
    stxt = (
        status_el.get_text(" ", strip=True).lower()
        if status_el
        else html.lower()[:6000]
    )
    if "завершён" in stxt or "завершен" in stxt or "полный текст" in stxt:
        extra["status"] = "завершён"
    elif "процессе" in stxt:
        extra["status"] = "в процессе"
    # Объём в словах/знаках (из "N зн." / "N а.л." панели статистики).
    stat = soup.get_text(" ", strip=True)
    mw = re.search(r"([\d\s]{4,})\s*зн", stat)
    if mw:
        chars = int(re.sub(r"\D", "", mw.group(1)) or 0)
        if chars:
            extra["words"] = chars // 6  # грубо: ~6 знаков на слово (рус.)
    return title, author, annotation, extra


def _parse_chapters(reader_html: str) -> list[dict]:
    m = _CHAPTERS_RE.search(reader_html)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return [
        {"id": ch.get("id"), "title": ch.get("title", "")} for ch in arr if ch.get("id")
    ]


def _build_epub(
    work_id: str,
    title: str,
    author: str,
    annotation: str,
    chapters: list[tuple[str, str]],
    cover: bytes | None = None,
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
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
