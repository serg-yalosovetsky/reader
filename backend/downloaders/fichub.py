"""Фоллбэк-загрузчик через публичный API FicHub (fichub.net).

Используется, когда FanFicFare не знает сайт. FicHub хорошо покрывает англоязычные
площадки (AO3, FFN, SpaceBattles, SufficientVelocity и пр.) и сам отдаёт готовый EPUB.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..app.config import FICHUB_API
from .base import DownloaderError, DownloadResult

_UA = "reader/0.1 (+https://github.com/serhii-yalosovetskyi/reader)"


def download(url: str) -> DownloadResult:
    """Скачать произведение через FicHub. Бросает DownloaderError при сбое."""
    try:
        with httpx.Client(timeout=120, headers={"User-Agent": _UA}, follow_redirects=True) as c:
            # 1) Запросить генерацию/ссылку на EPUB.
            r = c.get(f"{FICHUB_API}/epub", params={"q": url})
            if r.status_code != 200:
                raise DownloaderError(f"FicHub вернул {r.status_code} на {url}")
            data = r.json()
            epub_rel = data.get("epub_url") or data.get("urls", {}).get("epub")
            if not epub_rel:
                raise DownloaderError(f"FicHub не дал ссылку на EPUB для {url}")
            # epub_url относительный — добиваем хостом FicHub.
            base = f"{urlparse(FICHUB_API).scheme}://{urlparse(FICHUB_API).hostname}"
            epub_url = epub_rel if epub_rel.startswith("http") else base + epub_rel

            # 2) Скачать сам файл.
            fr = c.get(epub_url)
            if fr.status_code != 200 or not fr.content:
                raise DownloaderError(f"FicHub: не скачался EPUB ({fr.status_code})")
    except httpx.HTTPError as e:
        raise DownloaderError(f"FicHub сетевая ошибка: {e}") from e

    workdir = Path(tempfile.mkdtemp(prefix="fichub_"))
    epub = workdir / "book.epub"
    epub.write_bytes(fr.content)

    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    return DownloadResult(
        file_path=epub,
        file_format="epub",
        title=data.get("title") or meta.get("title", "") or "Без названия",
        author=data.get("author") or meta.get("author", ""),
        site=(urlparse(url).hostname or "").lower(),
        source_url=url,
        num_chapters=int(meta.get("chapters", 0) or 0),
        extra={"workdir": str(workdir), "fichub": data},
    )
