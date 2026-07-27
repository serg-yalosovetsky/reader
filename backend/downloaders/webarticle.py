"""Сборка книги из произвольных веб-статей: несколько ссылок → одна книга.

Зачем: разбор/лонгрид часто разбит на десятки постов блога («часть 2 из 23»).
Читать это в браузере неудобно, а в библиотеке хочется одну книгу с оглавлением
и картинками, доступную офлайн и через TTS.

Что делает модуль:
- `build_book(urls)` — качает каждую ссылку, вытаскивает читаемый текст
  (readability), скачивает картинки статьи и ВСТРАИВАЕТ их в EPUB, склеивает
  всё в одну книгу: одна ссылка = одна глава, порядок = порядок ссылок;
- `discover_parts(url)` — по ОДНОЙ ссылке на часть находит остальные части серии
  (sitemap сайта → поиск по сайту → навигация next/prev) и возвращает их
  отсортированными по номеру, чтобы пользователю не собирать 23 ссылки руками.

Картинки берутся только с той же статьи и складываются в EPUB как ресурсы
`images/iNNN.ext` — внешних ссылок в готовой книге не остаётся.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment

from .base import DownloaderError, DownloadResult
from .epub_build import build_epub

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 30.0

MAX_IMG_BYTES = 8 * 1024 * 1024        # одна картинка
MAX_TOTAL_IMG_BYTES = 60 * 1024 * 1024  # все картинки книги (MemoryMax сервиса = 1G)
MAX_IMAGES = 400
MAX_PARTS = 120                         # потолок на серию при автопоиске
MIN_ARTICLE_CHARS = 200

_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
}

# Мусор вокруг текста поста: шеринг, «похожие записи», подписка, комментарии.
_JUNK_RE = re.compile(
    r"sharedaddy|jp-relatedposts|jp-post-flair|sd-block|social|share-buttons?|"
    r"related-posts?|wpcnt|comment-respond|post-navigation|nav-links|"
    r"subscribe|newsletter|advert|banner|cookie",
    re.I,
)
_DROP_TAGS = (
    "script", "style", "noscript", "iframe", "form", "input", "button", "select",
    "textarea", "svg", "canvas", "object", "embed", "video", "audio", "link", "meta",
)
_KEEP_ATTRS = {"a": ("href", "title"), "img": ("src", "alt")}


def is_supported(url: str) -> bool:
    """Любая http(s)-ссылка: модуль — последний фоллбэк для «просто веб-страницы»."""
    return bool(re.match(r"^https?://", (url or "").strip(), re.I))


# --------------------------------------------------------------------------- #
#  сеть
# --------------------------------------------------------------------------- #
def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"},
        follow_redirects=True,
        timeout=TIMEOUT,
    )


def _get_html(client: httpx.Client, url: str) -> tuple[str, str]:
    """(html, final_url). Не-HTML отсекаем — иначе в книгу попадёт бинарь."""
    try:
        r = client.get(url)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise DownloaderError(f"{url}: не удалось загрузить ({type(e).__name__})") from e
    ct = r.headers.get("content-type", "")
    if ct and not any(t in ct for t in ("html", "xml", "text")):
        raise DownloaderError(f"{url}: это не веб-страница ({ct[:40]})")
    return _decode(r), str(r.url)


def _decode(r: httpx.Response) -> str:
    """Текст страницы с правильной кодировкой.

    Старые рунет-сайты отдают `Content-Type: text/html` без charset, а тело — в
    windows-1251. httpx в этом случае предполагает utf-8, и вся кириллица
    превращается в мусор — книга собиралась из нечитаемых символов. Порядок:
    charset из заголовка → <meta charset> в теле → utf-8, иначе cp1251.
    """
    enc = r.charset_encoding
    raw = r.content
    if not enc:
        m = re.search(rb"""charset=["']?\s*([\w-]+)""", raw[:4096], re.I)
        if m:
            enc = m.group(1).decode("ascii", "ignore")
    if not enc:
        try:
            raw.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            enc = "cp1251"
    try:
        return raw.decode(enc, "replace")
    except LookupError:  # выдуманное имя кодировки в meta
        return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
#  картинки
# --------------------------------------------------------------------------- #
class ImageStore:
    """Скачанные картинки книги: дедуп по URL и по содержимому, лимиты по объёму."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._by_url: dict[str, str] = {}
        self._by_hash: dict[str, str] = {}
        self.items: list[tuple[str, bytes, str]] = []  # (file_name, data, mime)
        self.total_bytes = 0
        self.skipped = 0

    def preload(self, url: str, data: bytes, mime: str) -> str | None:
        """Зарегистрировать картинку, пришедшую НЕ из сети (ресурс .mhtml).

        Нужна для сайтов, недоступных с VPS (LiveJournal): страницу сохраняет
        человек в браузере, а картинки едут внутри того же файла.
        """
        if not data or url in self._by_url:
            return self._by_url.get(url)
        mime = (mime or "").lower()
        if mime not in _MIME_EXT or len(data) > MAX_IMG_BYTES:
            return None
        digest = hashlib.sha1(data).hexdigest()
        if digest in self._by_hash:
            self._by_url[url] = self._by_hash[digest]
            return self._by_hash[digest]
        name = f"images/i{len(self.items) + 1:03d}.{_MIME_EXT[mime]}"
        self.items.append((name, data, mime))
        self.total_bytes += len(data)
        self._by_url[url] = name
        self._by_hash[digest] = name
        return name

    def add(self, url: str) -> str | None:
        """Скачать картинку и вернуть путь внутри EPUB (или None, если не вышло)."""
        if url in self._by_url:
            return self._by_url[url]
        if len(self.items) >= MAX_IMAGES or self.total_bytes >= MAX_TOTAL_IMG_BYTES:
            self.skipped += 1
            return None
        try:
            r = self._client.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.content
        except Exception:  # noqa: BLE001 — битая картинка не должна валить книгу
            self.skipped += 1
            return None
        if not data or len(data) > MAX_IMG_BYTES:
            self.skipped += 1
            return None
        mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if mime not in _MIME_EXT:
            mime = _sniff_mime(data)
        if not mime:
            self.skipped += 1
            return None

        digest = hashlib.sha1(data).hexdigest()
        if digest in self._by_hash:  # та же картинка под другим URL
            self._by_url[url] = self._by_hash[digest]
            return self._by_hash[digest]

        name = f"images/i{len(self.items) + 1:03d}.{_MIME_EXT[mime]}"
        self.items.append((name, data, mime))
        self.total_bytes += len(data)
        self._by_url[url] = name
        self._by_hash[digest] = name
        return name


def _sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"<?xml" or data[:4] == b"<svg":
        return "image/svg+xml"
    return ""


_WIX_MEDIA_RE = re.compile(
    r"^(https?://static\.wixstatic\.com/media/[\w.~-]+\.(?:png|jpe?g|webp|gif))", re.I
)


def _normalize_img_url(url: str) -> str:
    """Привести URL к «читаемой» версии картинки.

    Wix отдаёт в разметке обрезанный адрес превью (~3 КБ, мутное пятно), а по
    базовому адресу лежит оригинал на мегабайт. Просим у их CDN промежуточный
    размер: webp 1200px — 200-300 КБ, книга из 23 глав остаётся лёгкой.
    """
    m = _WIX_MEDIA_RE.match(url or "")
    if not m:
        return url
    base = m.group(1)
    name = base.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"{base}/v1/fit/w_1200,h_1200,q_80/{name}.webp"


TARGET_IMG_WIDTH = 1400


def _from_srcset(srcset: str) -> str:
    """Вариант картинки под ширину экрана читалки: самый большой из тех, что не
    шире TARGET_IMG_WIDTH (иначе — самый узкий). Оригиналы блогов бывают по
    несколько мегабайт, а в книге они всё равно ужимаются до ширины страницы."""
    variants: list[tuple[int, str]] = []
    for part in (srcset or "").split(","):
        bits = part.strip().split()
        if not bits or bits[0].startswith("data:"):
            continue
        width = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            width = int(re.sub(r"\D", "", bits[1]) or 0)
        variants.append((width, bits[0]))
    if not variants:
        return ""
    fitting = [v for v in variants if 0 < v[0] <= TARGET_IMG_WIDTH]
    if fitting:
        return max(fitting)[1]
    sized = [v for v in variants if v[0] > 0]
    return min(sized)[1] if sized else variants[0][1]


def _img_src(tag) -> str:
    """Настоящий URL картинки. Приоритет — умеренный размер из srcset; дальше
    ленивые data-атрибуты и обычный src (у WP там бывает заглушка, а оригинал
    в data-orig-file)."""
    from_set = _from_srcset(tag.get("srcset") or tag.get("data-srcset") or "")
    if from_set:
        return _normalize_img_url(from_set)
    for attr in ("data-src", "data-lazy-src", "src", "data-large-file", "data-orig-file"):
        v = (tag.get(attr) or "").strip()
        if v and not v.startswith("data:"):
            return _normalize_img_url(v)
    return ""


# --------------------------------------------------------------------------- #
#  разбор страницы
# --------------------------------------------------------------------------- #
@dataclass
class Article:
    url: str
    title: str = ""
    author: str = ""
    html: str = ""          # уже очищенный XHTML-фрагмент с локальными картинками
    chars: int = 0
    og_image: str = ""
    next_url: str = ""
    prev_url: str = ""
    lang: str = ""
    warnings: list[str] = field(default_factory=list)


def _site_name(soup: BeautifulSoup, url: str) -> str:
    m = soup.find("meta", property="og:site_name")
    if m and m.get("content"):
        return m["content"].strip()
    return urlparse(url).hostname or ""


def _page_title(soup: BeautifulSoup, url: str) -> str:
    for finder in (
        lambda: (soup.find("meta", property="og:title") or {}).get("content"),
        lambda: soup.find("h1", class_=re.compile(r"entry-title|post-title", re.I)),
        lambda: soup.find("h1"),
        lambda: soup.find("title"),
    ):
        try:
            got = finder()
        except Exception:  # noqa: BLE001
            got = None
        if not got:
            continue
        text = got if isinstance(got, str) else got.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if text:
            site = _site_name(soup, url)
            if site:  # «Заголовок – Название блога» → «Заголовок»
                text = re.sub(
                    r"\s*[|–—\-·]\s*" + re.escape(site) + r"\s*$", "", text, flags=re.I
                )
            return text.strip()
    return url


def _page_author(soup: BeautifulSoup) -> str:
    m = soup.find("meta", attrs={"name": "author"}) or soup.find(
        "meta", property="article:author"
    )
    if m and (m.get("content") or "").strip() and "http" not in (m.get("content") or ""):
        return m["content"].strip()
    for sel in ("a[rel=author]", ".author-name", ".byline .author", "span.author", ".entry-author"):
        el = soup.select_one(sel)
        if el:
            txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            txt = re.sub(r"^(by|автор)[\s:]+", "", txt, flags=re.I).strip()
            if 1 < len(txt) < 80:
                return txt
    return ""


def _nav_links(soup: BeautifulSoup, base: str) -> tuple[str, str]:
    """(next, prev) — по rel-ссылкам и типовой пост-навигации WordPress."""
    def _rel(rel: str) -> str:
        el = soup.find("link", rel=rel) or soup.select_one(f"a[rel~={rel}]")
        href = (el.get("href") if el else "") or ""
        return urljoin(base, href) if href else ""

    nxt, prev = _rel("next"), _rel("prev")
    if not nxt:
        el = soup.select_one(".nav-next a, .post-next a, .next-post a, a.next")
        if el and el.get("href"):
            nxt = urljoin(base, el["href"])
    if not prev:
        el = soup.select_one(".nav-previous a, .post-previous a, .prev-post a, a.prev")
        if el and el.get("href"):
            prev = urljoin(base, el["href"])
    return nxt, prev


def _node_with_images(full: BeautifulSoup, min_chars: int):
    """Минимальный узел, содержащий и текст статьи, и её картинки.

    Идём вверх от картинки в середине страницы (первая часто — логотип шапки)
    и останавливаемся на первом предке, у которого достаточно текста: это и есть
    контейнер поста. Дёшево — по глубине, а не по всем узлам документа.
    """
    imgs = full.find_all(["img", "wow-image"])
    if not imgs:
        return None
    anchor = imgs[len(imgs) // 2] if len(imgs) > 2 else imgs[0]
    for parent in anchor.parents:
        if parent.name in ("body", "html", "[document]"):
            break
        if len(parent.get_text(" ", strip=True)) >= min_chars:
            return parent
    return None


def _content_soup(html: str) -> BeautifulSoup:
    """Тело статьи без обвязки сайта. readability + фоллбэк на типовые контейнеры.

    Отдельный случай — SPA-вёрстка (Wix и подобные): readability вытаскивает
    текст, но теряет иллюстрации, потому что они лежат в кастомных элементах.
    Тогда берём узел, в котором есть и текст, и картинки.
    """
    full = BeautifulSoup(html, "lxml")
    summary_soup = None
    try:
        from readability import Document

        summary_soup = BeautifulSoup(Document(html).summary(html_partial=True), "lxml")
    except Exception:  # noqa: BLE001 — readability спотыкается на кривой вёрстке
        summary_soup = None
    s_len = len(summary_soup.get_text(" ", strip=True)) if summary_soup else 0

    if summary_soup and s_len >= MIN_ARTICLE_CHARS and summary_soup.find("img"):
        return summary_soup
    node = _node_with_images(full, max(int(s_len * 0.7), MIN_ARTICLE_CHARS))
    if node is not None:
        return BeautifulSoup(str(node), "lxml")
    if summary_soup and s_len >= MIN_ARTICLE_CHARS:
        return summary_soup
    node = full.select_one(
        "div.entry-content, div.post-content, article .content, article, main"
    )
    return BeautifulSoup(str(node) if node else html, "lxml")


def _sanitize(soup: BeautifulSoup, base_url: str, images: ImageStore) -> str:
    """Оставить текст и картинки, выбросить скрипты/виджеты/шеринг; картинки
    заменить на локальные ресурсы EPUB. Возвращает XHTML-фрагмент."""
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for el in soup.find_all(attrs={"class": _JUNK_RE}):
        el.decompose()
    for el in soup.find_all(attrs={"id": _JUNK_RE}):
        el.decompose()

    for img in soup.find_all("img"):
        src = _img_src(img)
        name = images.add(urljoin(base_url, src)) if src else None
        if not name:
            img.decompose()
            continue
        alt = img.get("alt") or ""
        img.attrs = {"src": name, "alt": alt}

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        a.attrs = {"href": urljoin(base_url, href)} if href else {}

    for el in soup.find_all(True):
        if el.name in _KEEP_ATTRS:
            continue
        el.attrs = {}

    return _to_xhtml(str(soup))


def _to_xhtml(html: str) -> str:
    """HTML-фрагмент → XHTML (самозакрывающиеся <img/>, <br/>): EPUB — это XML,
    и невалидный фрагмент ломает открытие книги в foliate."""
    try:
        import lxml.etree
        import lxml.html

        frag = lxml.html.fragment_fromstring(html or "<div></div>", create_parent="div")
        return lxml.etree.tostring(frag, encoding="unicode", method="xml")
    except Exception:  # noqa: BLE001
        return html


def fetch_article(client: httpx.Client, url: str, images: ImageStore) -> Article:
    """Скачать и разобрать одну статью (текст + картинки внутрь книги)."""
    html, final = _get_html(client, url)
    full = BeautifulSoup(html, "lxml")
    art = Article(url=final)
    art.title = _page_title(full, final)
    art.author = _page_author(full)
    og = full.find("meta", property="og:image")
    art.og_image = urljoin(final, og["content"]) if og and og.get("content") else ""
    art.next_url, art.prev_url = _nav_links(full, final)
    lang = (full.find("html") or {}).get("lang") if full.find("html") else ""
    art.lang = (lang or "").split("-")[0].lower()

    body = _content_soup(html)
    text_len = len(body.get_text(" ", strip=True))
    art.html = _sanitize(body, final, images)
    art.chars = text_len
    if text_len < MIN_ARTICLE_CHARS:
        art.warnings.append(f"{final}: почти нет текста ({text_len} симв.)")
    return art


# --------------------------------------------------------------------------- #
#  сборка книги
# --------------------------------------------------------------------------- #
def _series_title(titles: list[str]) -> str:
    """Общее название серии по заголовкам частей: общий префикс слов минус
    хвостовые «Episode/Part/Глава»."""
    if not titles:
        return ""
    if len(titles) == 1:
        return titles[0]
    words = [t.split() for t in titles]
    common: list[str] = []
    for i in range(min(len(w) for w in words)):
        tok = words[0][i]
        if all(w[i].lower() == tok.lower() for w in words):
            common.append(tok)
        else:
            break
    while common and re.fullmatch(
        r"(episode|ep|part|chapter|pt|no|№|часть|глава|серия)[.:\-–—]?", common[-1], re.I
    ):
        common.pop()
    out = re.sub(r"[\s:–—\-|,]+$", "", " ".join(common)).strip()
    return out or titles[0]


def _series_from_title(title: str) -> str:
    """Название серии из заголовка одной части: всё до номера, без «Episode»."""
    if not title:
        return ""
    words = title.split()
    cut = len(words)
    for i, wd in enumerate(words):
        if re.search(r"\d", wd):
            cut = i
            break
    head = words[:cut]
    while head and re.fullmatch(
        r"(episode|ep|part|chapter|pt|no|№|часть|глава|серия)[.:\-–—]?", head[-1], re.I
    ):
        head.pop()
    out = re.sub(r"[\s:–—\-|,]+$", "", " ".join(head)).strip()
    return out or title


def build_book(
    urls: list[str],
    title: str = "",
    author: str = "",
    progress_cb=None,
) -> DownloadResult:
    """Собрать одну книгу из списка ссылок: ссылка = глава, порядок сохраняется."""
    urls = [u.strip() for u in urls if (u or "").strip()]
    if not urls:
        raise DownloaderError("не передано ни одной ссылки")
    if len(urls) > MAX_PARTS:
        raise DownloaderError(f"слишком много ссылок (>{MAX_PARTS})")

    sections: list[tuple[str | None, str]] = []
    warnings: list[str] = []
    arts: list[Article] = []
    with make_client() as client:
        images = ImageStore(client)
        for i, u in enumerate(urls, 1):
            if progress_cb:
                progress_cb(i, len(urls), u)
            try:
                art = fetch_article(client, u, images)
            except DownloaderError as e:
                warnings.append(str(e))
                continue
            if art.chars < MIN_ARTICLE_CHARS:
                warnings.extend(art.warnings)
                if art.chars == 0:
                    continue
            arts.append(art)
            sections.append((art.title or f"Часть {i}", art.html))

        if not sections:
            raise DownloaderError(
                "ни из одной ссылки не удалось извлечь текст "
                + ("; ".join(warnings[:3]) if warnings else "")
            )

        cover = None
        for art in arts:
            if art.og_image:
                try:
                    r = client.get(art.og_image, timeout=TIMEOUT)
                    if r.status_code == 200 and len(r.content) <= MAX_IMG_BYTES:
                        cover = r.content
                        break
                except Exception:  # noqa: BLE001 — обложка не критична
                    pass
        # og:image есть не у всех блогов (github.io, старый blogspot). Тогда
        # обложка — первая иллюстрация книги: она с той же страницы, то есть по
        # теме, и это честнее пустой карточки или ИИ-картинки по описанию.
        if not cover:
            for _name, data, mime in images.items:
                # иконки интерфейса и аватарки в обложку не годятся
                if mime != "image/svg+xml" and len(data) >= 20_000:
                    cover = data
                    break

    book_title = (title or "").strip() or _series_title([a.title for a in arts])
    book_author = (author or "").strip() or next((a.author for a in arts if a.author), "")
    host = urlparse(arts[0].url).hostname or ""
    lang = next((a.lang for a in arts if a.lang), "") or "ru"
    ident = "web-" + hashlib.sha1("\n".join(a.url for a in arts).encode()).hexdigest()[:16]
    annotation = f"Собрано из {len(sections)} статей с {host}."

    path: Path = build_epub(
        ident,
        book_title,
        book_author,
        sections,
        lang=lang,
        annotation=annotation,
        cover=cover,
        images=images.items,
    )
    return DownloadResult(
        file_path=path,
        file_format="epub",
        title=book_title,
        author=book_author,
        site=host,
        source_url=arts[0].url,
        num_chapters=len(sections),
        extra={
            "annotation": annotation,
            "web_parts": [a.url for a in arts],
            "images": len(images.items),
            "images_skipped": images.skipped,
            "warnings": warnings,
        },
    )


def parse_mhtml(path: str | Path) -> tuple[str, str, list[tuple[str, bytes, str]]]:
    """Разобрать сохранённую браузером страницу (.mhtml/.mht).

    Возвращает (исходный URL, html, [(url ресурса, байты, mime)]). Формат — MIME
    multipart/related, поэтому читается стандартным `email`: первая text/html
    часть — сама страница, image/* — её картинки.
    """
    import email
    from email import policy

    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    base = (msg.get("Snapshot-Content-Location") or "").strip()
    html = ""
    resources: list[tuple[str, bytes, str]] = []
    for part in msg.walk():
        ct = (part.get_content_type() or "").lower()
        if ct == "text/html" and not html:
            raw = part.get_payload(decode=True) or b""
            html = raw.decode(part.get_content_charset() or "utf-8", "replace")
            base = base or (part.get("Content-Location") or "").strip()
        elif ct.startswith("image/"):
            loc = (part.get("Content-Location") or "").strip()
            data = part.get_payload(decode=True) or b""
            if loc and data:
                resources.append((loc, data, ct))
    if not html:
        raise DownloaderError(f"{Path(path).name}: в файле нет HTML-страницы")
    return base or f"file://{Path(path).name}", html, resources


def build_book_from_files(
    paths: list[str | Path],
    title: str = "",
    author: str = "",
    progress_cb=None,
) -> DownloadResult:
    """Книга из страниц, сохранённых человеком в браузере (.mhtml/.html).

    Путь для источников, недоступных с сервера (LiveJournal блокирует, антибот,
    закрытый доступ): страницу открывает и сохраняет Серж, а сборка — та же, что
    и для живых ссылок, включая картинки из сохранённого файла.
    """
    sections: list[tuple[str | None, str]] = []
    warnings: list[str] = []
    arts: list[Article] = []
    with make_client() as client:
        images = ImageStore(client)
        for i, path in enumerate(paths, 1):
            if progress_cb:
                progress_cb(i, len(paths), str(path))
            try:
                if str(path).lower().endswith((".mhtml", ".mht")):
                    src_url, html, resources = parse_mhtml(path)
                else:
                    html = Path(path).read_bytes().decode("utf-8", "replace")
                    src_url, resources = f"file://{Path(path).name}", []
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{Path(path).name}: {type(e).__name__}")
                continue
            for loc, data, mime in resources:
                images.preload(loc, data, mime)

            full = BeautifulSoup(html, "lxml")
            art = Article(url=src_url)
            art.title = _page_title(full, src_url)
            art.author = _page_author(full)
            lang = (full.find("html") or {}).get("lang") if full.find("html") else ""
            art.lang = (lang or "").split("-")[0].lower()
            body = _content_soup(html)
            art.chars = len(body.get_text(" ", strip=True))
            art.html = _sanitize(body, src_url, images)
            if art.chars < MIN_ARTICLE_CHARS:
                warnings.append(f"{Path(path).name}: почти нет текста ({art.chars} симв.)")
                if not art.chars:
                    continue
            arts.append(art)
            sections.append((art.title or f"Часть {i}", art.html))

        if not sections:
            raise DownloaderError("ни из одного файла не удалось извлечь текст")

        cover = None
        for _name, data, mime in images.items:
            if mime != "image/svg+xml" and len(data) >= 20_000:
                cover = data
                break

    book_title = (title or "").strip() or _series_title([a.title for a in arts])
    book_author = (author or "").strip() or next((a.author for a in arts if a.author), "")
    host = urlparse(arts[0].url).hostname or ""
    lang = next((a.lang for a in arts if a.lang), "") or "ru"
    ident = "web-" + hashlib.sha1("\n".join(a.url for a in arts).encode()).hexdigest()[:16]
    annotation = f"Собрано из {len(sections)} сохранённых страниц" + (f" ({host})" if host else "") + "."

    path_out: Path = build_epub(
        ident, book_title, book_author, sections,
        lang=lang, annotation=annotation, cover=cover, images=images.items,
    )
    return DownloadResult(
        file_path=path_out,
        file_format="epub",
        title=book_title,
        author=book_author,
        site=host,
        source_url=arts[0].url,
        num_chapters=len(sections),
        extra={
            "annotation": annotation,
            "web_parts": [a.url for a in arts],
            "images": len(images.items),
            "images_skipped": images.skipped,
            "warnings": warnings,
        },
    )


def download(url: str) -> DownloadResult:
    """Одна произвольная веб-страница → книга (фоллбэк цепочки загрузчиков)."""
    return build_book([url])


# --------------------------------------------------------------------------- #
#  поиск остальных частей серии
# --------------------------------------------------------------------------- #
def _slug(url: str) -> str:
    segs = [s for s in urlparse(url).path.split("/") if s]
    return segs[-1].lower() if segs else ""


def _series_pattern(url: str) -> tuple[str, int] | None:
    """(префикс slug'а до номера, номер этой части). None — номера в slug нет."""
    slug = _slug(url)
    m = re.search(r"\d+", slug)
    if not m:
        return None
    prefix = slug[: m.start()]
    if len(prefix.strip("-_")) < 3:  # слишком широкий шаблон — поймает пол-сайта
        return None
    return prefix, int(m.group())


def _part_num(url: str, prefix: str) -> int | None:
    slug = _slug(url)
    if not slug.startswith(prefix):
        return None
    m = re.match(r"\d+", slug[len(prefix):])
    return int(m.group()) if m else None


def _sitemap_candidates(client: httpx.Client, root: str, prefix: str) -> list[str]:
    """URL постов из sitemap сайта, подходящие под шаблон серии."""
    seen: set[str] = set()
    found: list[str] = []
    queue = [
        urljoin(root, p)
        for p in ("/wp-sitemap.xml", "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")
    ]
    files = 0
    while queue and files < 25:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = client.get(sm, timeout=TIMEOUT)
            if r.status_code != 200 or "xml" not in r.headers.get("content-type", "xml"):
                continue
            body = r.text
        except Exception:  # noqa: BLE001
            continue
        files += 1
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        is_index = "<sitemapindex" in body
        for loc in locs:
            if is_index:
                # вложенные карты постов; страницы/категории/теги не нужны
                if re.search(r"(post|article|entr)", loc, re.I) or len(locs) <= 6:
                    queue.append(loc)
            elif _part_num(loc, prefix) is not None:
                found.append(loc)
        if len(found) >= MAX_PARTS:
            break
    return found


def _taxonomy_candidates(
    client: httpx.Client, soup: BeautifulSoup, base: str, prefix: str
) -> list[str]:
    """Части серии со страниц тега/рубрики, на которые ссылается сама статья.

    Надёжнее sitemap: у Jetpack/WP карта сайта обрезана первой тысячей адресов,
    и хвост серии в неё не попадает, а архив тега перечисляет ровно посты серии.
    """
    taxes: list[str] = []
    for a in soup.find_all("a", href=True):
        loc = urljoin(base, a["href"])
        if re.search(r"/(tag|category|tags|topics|series)/", loc) and loc not in taxes:
            taxes.append(loc.split("#")[0])
    out: list[str] = []
    for tax in taxes[:6]:
        for page in range(1, 8):
            url = tax if page == 1 else urljoin(tax.rstrip("/") + "/", f"page/{page}/")
            try:
                r = client.get(url, timeout=TIMEOUT)
                if r.status_code != 200:
                    break
                page_soup = BeautifulSoup(r.text, "lxml")
            except Exception:  # noqa: BLE001
                break
            hits = 0
            for a in page_soup.find_all("a", href=True):
                loc = urljoin(url, a["href"])
                if _part_num(loc, prefix) is not None:
                    out.append(loc)
                    hits += 1
            if not hits:
                break
        if len({_part_num(u, prefix) for u in out}) >= MAX_PARTS:
            break
    return out


def _search_candidates(client: httpx.Client, root: str, prefix: str) -> list[str]:
    """Фоллбэк: встроенный поиск сайта (WordPress `?s=`)."""
    words = " ".join(w for w in prefix.strip("-_").split("-") if w)
    out: list[str] = []
    for page in (1, 2, 3):
        q = quote_plus(words)
        url = urljoin(root, f"/?s={q}" if page == 1 else f"/page/{page}/?s={q}")
        try:
            r = client.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:  # noqa: BLE001
            break
        hits = 0
        for a in soup.find_all("a", href=True):
            loc = urljoin(root, a["href"])
            if _part_num(loc, prefix) is not None:
                out.append(loc)
                hits += 1
        if not hits:
            break
    return out


def _nav_walk(client: httpx.Client, start: str, prefix: str) -> list[str]:
    """Последний фоллбэк: идём по next/prev от исходной статьи. Между частями
    серии в блоге бывают чужие посты, поэтому не обрываемся на первом
    несовпадении, но и не бродим по сайту бесконечно."""
    out: list[str] = []
    for direction in ("next_url", "prev_url"):
        url, miss, steps = start, 0, 0
        while url and steps < 40 and miss < 8:
            try:
                html, final = _get_html(client, url)
            except DownloaderError:
                break
            soup = BeautifulSoup(html, "lxml")
            nxt, prev = _nav_links(soup, final)
            url = nxt if direction == "next_url" else prev
            steps += 1
            if url and _part_num(url, prefix) is not None:
                out.append(url)
                miss = 0
            else:
                miss += 1
    return out


def discover_parts(url: str, limit: int = MAX_PARTS) -> dict:
    """Найти все части серии по ссылке на одну из них.

    Возвращает {series, parts:[{url,num}], source, note}. Шаблон берётся из
    slug'а («…/ergo-proxy-episode-02-confessions…» → «ergo-proxy-episode-» + 02),
    кандидаты ищутся в sitemap, затем поиском по сайту, затем обходом next/prev.
    """
    if not is_supported(url):
        raise DownloaderError("нужна http(s)-ссылка")
    pat = _series_pattern(url)
    with make_client() as client:
        html, final = _get_html(client, url)
        soup = BeautifulSoup(html, "lxml")
        title = _page_title(soup, final)
        pat = pat or _series_pattern(final)
        if not pat:
            return {
                "series": title,
                "parts": [{"url": final, "num": 1, "title": title}],
                "source": "single",
                "note": "в адресе нет номера части — собрать серию автоматически не вышло",
            }
        prefix, _cur = pat
        root = f"{urlparse(final).scheme}://{urlparse(final).hostname}"

        cands = [final]
        sources: list[str] = []
        # ссылки с самой страницы (оглавление серии, перекрёстные упоминания)
        for a in soup.find_all("a", href=True):
            loc = urljoin(final, a["href"])
            if _part_num(loc, prefix) is not None:
                cands.append(loc)
                if "page" not in sources:
                    sources.append("page")
        got = _sitemap_candidates(client, root, prefix)
        if got:
            sources.append("sitemap")
            cands += got
        got = _taxonomy_candidates(client, soup, final, prefix)
        if got:
            sources.append("tag")
            cands += got
        if len({_part_num(c, prefix) for c in cands}) < 3:
            got = _search_candidates(client, root, prefix)
            if got:
                sources.append("search")
                cands += got
        if len({_part_num(c, prefix) for c in cands}) < 3:
            got = _nav_walk(client, final, prefix)
            if got:
                sources.append("nav")
                cands += got
        source = "+".join(sources) or "single"

    by_num: dict[int, str] = {}
    for c in cands:
        # Хвостовой слэш добавляем только «каталожным» адресам (WordPress).
        # Для blogspot и прочих «…/post.html» слэш в конце даёт 404 — так все
        # 18 найденных эпизодов молча отваливались при сборке.
        c = c.split("#")[0]
        if not re.search(r"\.\w{2,5}$", urlparse(c).path):
            c = c.rstrip("/") + "/"
        n = _part_num(c, prefix)
        if n is None or (urlparse(c).hostname != urlparse(final).hostname):
            continue
        by_num.setdefault(n, c)
    parts = [{"url": u, "num": n} for n, u in sorted(by_num.items())][:limit]
    nums = [p["num"] for p in parts]
    note = ""
    if len(parts) < 2:
        note = "других частей не нашлось — соберём одну статью"
    else:
        gaps = [n for n in range(min(nums), max(nums) + 1) if n not in set(nums)]
        note = f"части {min(nums)}–{max(nums)}"
        if gaps:
            note += f"; не найдены: {', '.join(str(g) for g in gaps[:12])}"
        note += ". Если продолжение серии живёт на другом сайте — допишите его ссылки в список."
    return {
        "series": _series_from_title(title) if len(parts) > 1 else title,
        "parts": parts,
        "source": source if len(parts) > 1 else "single",
        "note": note,
    }
