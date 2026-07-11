"""Извлечение обложки из EPUB (OPF) и FB2 (<coverpage>/<binary>). Кладёт в COVERS_DIR."""

from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import zipfile
from pathlib import Path

from .config import COVERS_DIR

# ---------------- чёрный список дженерик-«обложек» ----------------
# Некоторые источники на страницу фика БЕЗ своей обложки отдают одну и ту же
# заглушку-баннер сайта (og:image). Такую картинку НЕ считаем обложкой — иначе
# все безобложечные фики выглядят одинаково («Мир фанфикшена…») и не получают
# ИИ-генерацию. Матчим по md5 точного файла (заглушки байт-в-байт идентичны).
# Пополняемо: добавляй md5 новой заглушки, если всплывёт другой источник.
_GENERIC_COVER_MD5 = {
    "fe79f62359104fd5d03da79e4b8c9774",  # ficbook.net дженерик-баннер, 69618 б
    "9173cbd4f0e3c7757e27fa5ec5a982dd",  # Calibre «нет обложки», 19501 б (282×400)
}


def is_generic_cover(data: bytes | None, *, check_aspect: bool = False) -> bool:
    """True, если байты — не настоящая обложка книги, а заглушка-баннер источника.

    Два сигнала:
    • точный md5 известной заглушки (байт-в-байт идентичны у одного источника);
    • ``check_aspect`` — форма кадра: дженерик og:image соцсетей альбомный
      (~1200×630), а обложка книги всегда портретная. Явно альбомную картинку
      из веб-источника обложкой не считаем. Для встроенных в EPUB не включаем
      (там кадр гарантированно обложечный, лишний риск ложняка не нужен).
    """
    if not data:
        return False
    if hashlib.md5(data).hexdigest() in _GENERIC_COVER_MD5:
        return True
    if check_aspect:
        size = _img_size(data)
        if size:
            w, h = size
            if w > h * 1.15:  # заметно шире, чем выше → баннер, не обложка
                return True
    return False


def _img_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) для JPEG/PNG/GIF/WebP без внешних зависимостей. None, если
    формат не распознан или заголовок битый."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":  # IHDR сразу за сигнатурой
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h) if w and h else None
        if data[:3] == b"GIF":
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return (w, h) if w and h else None
        if data[:2] == b"\xff\xd8":  # JPEG — идём по сегментам до SOF-маркера
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(data[i + 5 : i + 7], "big")
                    w = int.from_bytes(data[i + 7 : i + 9], "big")
                    return (w, h) if w and h else None
                seg = int.from_bytes(data[i + 2 : i + 4], "big")
                if seg <= 0:
                    break
                i += 2 + seg
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = data[12:16]
            if fmt == b"VP8 ":
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
            if fmt == b"VP8L":
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = ((b1 & 0x3F) << 8 | b0) + 1
                h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
                return (w, h)
            if fmt == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return (w, h)
    except Exception:  # noqa: BLE001 — размер не критичен, просто не фильтруем
        return None
    return None


def extract_cover(file_path: str | Path, fmt: str, sha1: str) -> Path | None:
    """Вытащить обложку и сохранить как COVERS_DIR/<sha1>.<ext>. None, если нет."""
    try:
        data = _epub_cover(file_path) if fmt == "epub" else _fb2_cover(file_path)
    except Exception:  # noqa: BLE001 — обложка не критична
        data = None
    if not data or is_generic_cover(data):
        return None
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    out = COVERS_DIR / f"{sha1}{_img_ext(data)}"
    out.write_bytes(data)
    return out


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_cover_bytes(source_url: str) -> bytes | None:
    """Скачать байты обложки со страницы-источника по og:image. None при неудаче."""
    if not source_url:
        return None
    try:
        html = _fetch(source_url, _UA)
        if not html:
            return None
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html,
        )
        if not m:
            return None
        img_url = m.group(1)
        # ficbook отдаёт в og:image АЛЬБОМНЫЙ соц-кроп обложки (`/fanfic-covers/m_…`,
        # 600×400 → отсекся бы как баннер). Настоящая обложка фика — портретная
        # display-версия `/d_…` (400×600). Подменяем префикс, чтобы взять её.
        # Дженерик-заглушку (`/assets/design/…socials.png`) это не трогает — она
        # не в /fanfic-covers/ и отсекается по форме кадра как раньше.
        if "/fanfic-covers/" in img_url:
            img_url = re.sub(r"/m_", "/d_", img_url, count=1)
        data = _fetch(img_url, _UA, binary=True, base=source_url)
        return data if data and len(data) > 200 else None
    except Exception:  # noqa: BLE001
        return None


def save_cover_bytes(data: bytes, sha1: str) -> Path | None:
    if not data or is_generic_cover(data):
        return None
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    out = COVERS_DIR / f"{sha1}{_img_ext(data)}"
    if out.exists() and out.stat().st_size == len(data):
        return out  # обложка не изменилась — не перезаписываем
    out.write_bytes(data)
    return out


def _mirror_urls(title: str, author: str, source_url: str) -> list[str]:
    """URL-страниц книги на разных сайтах-зеркалах. У одной книги обложка бывает
    только на одном из них (нет на ficbook — есть на author.today, и наоборот).
    Порядок: исходный источник, затем поиск по названию/автору на других сайтах."""
    seen: set[str] = set()
    out: list[str] = []

    def add(u: str | None) -> None:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    add(source_url)
    if title:
        try:
            from ..downloaders import authortoday

            add(authortoday.search_work(title, author or ""))
        except Exception:  # noqa: BLE001 — зеркало не нашлось, не критично
            pass
        try:
            from ..downloaders import searchfloor

            bid = searchfloor.search_book(title, author or "")
            if bid:
                add(f"https://searchfloor.org/b/{bid}")
        except Exception:  # noqa: BLE001
            pass
    return out


def fetch_source_cover(
    source_url: str, sha1: str, title: str = "", author: str = ""
) -> Path | None:
    """Скачать настоящую обложку с сайта-источника или его зеркал и сохранить
    файлом (для cover_path). Перебирает исходный URL и найденные зеркала книги на
    других сайтах; берёт первую НЕ дженерик-баннер картинку (см. is_generic_cover
    с check_aspect). title/author нужны, чтобы искать книгу на зеркалах."""
    for url in _mirror_urls(title, author, source_url):
        data = fetch_cover_bytes(url)
        if data and not is_generic_cover(data, check_aspect=True):
            p = save_cover_bytes(data, sha1)
            if p:
                return p
    return None


def generate_cover(
    meta: dict, sha1: str, salt: str = "", provider: str | None = None
) -> Path | None:
    """Сгенерировать обложку ИИ (ComfyUI/Pollinations/OpenAI) и сохранить файлом.

    ``meta`` — поля книги (title/author/genres/description/fandom). ``provider``
    перекрывает глобальный дефолт (батч зовёт с provider='comfy'). None, если
    генерация выключена или провайдер не отдал картинку."""
    from . import imagegen

    data = imagegen.generate({**meta, "sha1": sha1}, salt=salt, provider=provider)
    if not data or len(data) < 500:
        return None
    return save_cover_bytes(data, sha1)


def _fetch(url: str, ua: str, binary: bool = False, base: str = ""):
    from urllib.parse import urljoin, urlparse

    if base and not url.startswith("http"):
        url = urljoin(base, url)
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("ficbook.net"):
        import cloudscraper

        c = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows"}
        )
        r = c.get(url, timeout=40)
        return r.content if binary else r.text
    import httpx

    with httpx.Client(
        timeout=40, follow_redirects=True, headers={"User-Agent": ua}
    ) as c:
        r = c.get(url)
        return r.content if binary else r.text


def _img_ext(b: bytes) -> str:
    if b[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if b[:4] == b"GIF8":
        return ".gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _epub_cover(path) -> bytes | None:
    with zipfile.ZipFile(path) as z:
        try:
            container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        except KeyError:
            return None
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            return None
        opf_path = m.group(1)
        opf = z.read(opf_path).decode("utf-8", "ignore")
        opf_dir = posixpath.dirname(opf_path)

        href = None
        m = re.search(
            r'<meta[^>]+name=["\']cover["\'][^>]+content=["\']([^"\']+)', opf
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']cover["\']', opf
        )
        if m:
            cid = re.escape(m.group(1))
            mm = re.search(
                r'<item[^>]+id=["\']%s["\'][^>]+href=["\']([^"\']+)' % cid, opf
            ) or re.search(
                r'<item[^>]+href=["\']([^"\']+)["\'][^>]+id=["\']%s["\']' % cid, opf
            )
            if mm:
                href = mm.group(1)
        if not href:
            mm = re.search(
                r'<item[^>]+properties=["\'][^"\']*cover-image[^"\']*["\'][^>]+href=["\']([^"\']+)',
                opf,
            )
            if mm:
                href = mm.group(1)
        if not href:
            for mm in re.finditer(
                r'<item[^>]+href=["\']([^"\']+\.(?:jpe?g|png|webp))["\']', opf
            ):
                if "cover" in mm.group(1).lower():
                    href = mm.group(1)
                    break
        if not href:
            return None
        full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
        try:
            return z.read(full)
        except KeyError:
            return None


def _fb2_cover(path) -> bytes | None:
    text = Path(path).read_bytes().decode("utf-8", "ignore")
    m = re.search(r"<coverpage>(.*?)</coverpage>", text, re.S | re.I)
    if not m:
        return None
    mm = re.search(r'href="#?([^"]+)"', m.group(1))
    if not mm:
        return None
    bid = re.escape(mm.group(1))
    mb = re.search(r'<binary[^>]+id="%s"[^>]*>(.*?)</binary>' % bid, text, re.S)
    if not mb:
        return None
    try:
        return base64.b64decode(re.sub(r"\s+", "", mb.group(1)))
    except Exception:  # noqa: BLE001
        return None


def _epub_description(path) -> str:
    """Извлечь DC:description из EPUB OPF."""
    import html as _html

    try:
        with zipfile.ZipFile(path) as z:
            try:
                container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
            except KeyError:
                return ""
            m = re.search(r'full-path="([^"]+)"', container)
            if not m:
                return ""
            opf = z.read(m.group(1)).decode("utf-8", "ignore")
            desc_m = re.search(
                r"<dc:description[^>]*>(.*?)</dc:description>", opf, re.S | re.I
            )
            if not desc_m:
                return ""
            return _html.unescape(re.sub(r"<[^>]+>", " ", desc_m.group(1))).strip()
    except Exception:  # noqa: BLE001
        return ""


_COVER_URL_RE = re.compile(
    r"[Оо]бложк[аиаи]"
    r"\s*[-–—:]\s*(https?://[^\s<>\"')]+)",
    re.IGNORECASE,
)


def cover_from_description(description: str, sha1: str) -> "Path | None":
    """Ищет в описании книги URL обложки ('Обложка - https://...') и качает его."""
    if not description:
        return None
    m = _COVER_URL_RE.search(description)
    if not m:
        return None
    url = m.group(1).rstrip(".,;)")
    # Попытка 1: прямое скачивание (если URL ведёт на изображение напрямую)
    try:
        data = _fetch(url, _UA, binary=True)
        if data and len(data) > 500:
            magic = data[:4]
            if (
                magic[:2] == b"\xff\xd8"
                or magic == b"\x89PNG"
                or magic[:3] == b"GIF"
                or magic == b"RIFF"
            ):
                return save_cover_bytes(data, sha1)
    except Exception:  # noqa: BLE001
        pass
    # Попытка 2: og:image со страницы (работает для Google Photos, Vk, etc.)
    data = fetch_cover_bytes(url)
    if data and len(data) > 500:
        return save_cover_bytes(data, sha1)
    return None


def extract_pdf_cover(pdf_path: str | Path, sha1: str) -> Path | None:
    """Обложка PDF-книги — первая страница, отрендеренная через pdftoppm (poppler).
    Нужно для книг из Calibre, у которых обложка не сохранена как файл (OPDS её не
    отдаёт), но по факту это первая страница PDF (типично для Packt/O'Reilly)."""
    import subprocess
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "cover")
            subprocess.run(
                ["pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-r", "110",
                 "-singlefile", str(pdf_path), out],
                check=True, timeout=90, capture_output=True,
            )
            jpg = Path(out + ".jpg")
            if jpg.exists():
                return save_cover_bytes(jpg.read_bytes(), sha1)
    except Exception:  # noqa: BLE001 — обложка не критична
        return None
    return None
