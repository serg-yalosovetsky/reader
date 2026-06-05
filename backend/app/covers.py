"""Извлечение обложки из EPUB (OPF) и FB2 (<coverpage>/<binary>). Кладёт в COVERS_DIR."""
from __future__ import annotations

import base64
import posixpath
import re
import zipfile
from pathlib import Path

from .config import COVERS_DIR


def extract_cover(file_path: str | Path, fmt: str, sha1: str) -> Path | None:
    """Вытащить обложку и сохранить как COVERS_DIR/<sha1>.<ext>. None, если нет."""
    try:
        data = _epub_cover(file_path) if fmt == "epub" else _fb2_cover(file_path)
    except Exception:  # noqa: BLE001 — обложка не критична
        data = None
    if not data:
        return None
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    out = COVERS_DIR / f"{sha1}{_img_ext(data)}"
    out.write_bytes(data)
    return out


def fetch_source_cover(source_url: str, sha1: str) -> Path | None:
    """Скачать обложку со страницы-источника по og:image (есть у большинства сайтов).
    ficbook закрыт анти-ботом → cloudscraper."""
    if not source_url:
        return None
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        html = _fetch(source_url, ua)
        if not html:
            return None
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html))
        if not m:
            return None
        img_url = m.group(1)
        data = _fetch(img_url, ua, binary=True, base=source_url)
        if not data or len(data) < 200:
            return None
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        out = COVERS_DIR / f"{sha1}{_img_ext(data)}"
        out.write_bytes(data)
        return out
    except Exception:  # noqa: BLE001
        return None


def _fetch(url: str, ua: str, binary: bool = False, base: str = ""):
    from urllib.parse import urljoin, urlparse
    if base and not url.startswith("http"):
        url = urljoin(base, url)
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("ficbook.net"):
        import cloudscraper
        c = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
        r = c.get(url, timeout=40)
        return r.content if binary else r.text
    import httpx
    with httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": ua}) as c:
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
        m = (re.search(r'<meta[^>]+name=["\']cover["\'][^>]+content=["\']([^"\']+)', opf)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']cover["\']', opf))
        if m:
            cid = re.escape(m.group(1))
            mm = (re.search(r'<item[^>]+id=["\']%s["\'][^>]+href=["\']([^"\']+)' % cid, opf)
                  or re.search(r'<item[^>]+href=["\']([^"\']+)["\'][^>]+id=["\']%s["\']' % cid, opf))
            if mm:
                href = mm.group(1)
        if not href:
            mm = re.search(r'<item[^>]+properties=["\'][^"\']*cover-image[^"\']*["\'][^>]+href=["\']([^"\']+)', opf)
            if mm:
                href = mm.group(1)
        if not href:
            for mm in re.finditer(r'<item[^>]+href=["\']([^"\']+\.(?:jpe?g|png|webp))["\']', opf):
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
