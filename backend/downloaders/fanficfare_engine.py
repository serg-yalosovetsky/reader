"""Скачивание через FanFicFare (ficbook.net, fanfics.me, AO3, fanfiction.net и
сотни других сайтов). FanFicFare запускается в subprocess для изоляции памяти —
важно на VPS с дефицитом RAM: процесс отрабатывает и освобождает всё разом.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .base import DownloaderError, DownloadResult, UnsupportedURL

# Домены, которые FanFicFare покрывает и которые нам интересны в первую очередь.
# Список не исчерпывающий: FanFicFare поддерживает 100+ сайтов, но маршрутизацию
# делаем по известным нам, остальное уходит в FicHub-фоллбэк.
KNOWN_DOMAINS = {
    "ficbook.net": "ficbook",
    "fanfics.me": "fanfics",
    "archiveofourown.org": "ao3",
    "www.fanfiction.net": "ffn",
    "fanfiction.net": "ffn",
    "m.fanfiction.net": "ffn",
}


def _site_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return KNOWN_DOMAINS.get(host, host)


def supports(url: str) -> bool:
    """Быстрая проверка по домену (без запуска FanFicFare)."""
    host = (urlparse(url).hostname or "").lower()
    return host in KNOWN_DOMAINS


def _ff_executable() -> list[str]:
    """Команда запуска FanFicFare. Через -c и cli.main, чтобы не зависеть от PATH
    и работать одинаково в venv на Windows и на Linux-VPS."""
    return [sys.executable, "-c", "from fanficfare.cli import main; main()"]


def download(url: str, *, is_adult: bool = True, extra_options: dict | None = None) -> DownloadResult:
    """Скачать произведение в EPUB. Возвращает DownloadResult.

    Бросает UnsupportedURL, если FanFicFare не знает сайт (тогда цепочка пробует
    следующий загрузчик), или DownloaderError при иных сбоях.
    """
    workdir = Path(tempfile.mkdtemp(prefix="fff_"))
    cmd = _ff_executable() + [
        "-f", "epub",
        "--json-meta-file",          # метаданные рядом: <output>.json
        "--non-interactive",
        "-o", f"is_adult={'true' if is_adult else 'false'}",
        "-o", "output_filename=book.${formatext}",
    ]
    for k, v in (extra_options or {}).items():
        cmd += ["-o", f"{k}={v}"]
    cmd.append(url)

    try:
        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise DownloaderError(f"FanFicFare превысил тайм-аут на {url}") from e

    stderr = (proc.stderr or "").strip()
    # FanFicFare сообщает о незнакомом сайте характерным текстом.
    if "Failed to find adapter" in stderr or "No adapter found" in stderr:
        raise UnsupportedURL(stderr or f"FanFicFare не знает сайт: {url}")

    epubs = sorted(workdir.glob("*.epub"))
    if not epubs:
        # Нет файла — частые причины: требуется логин, защита Cloudflare, 0 глав.
        msg = stderr or (proc.stdout or "").strip() or "EPUB не создан"
        raise DownloaderError(f"Не удалось скачать {url}: {msg[:400]}")

    epub = epubs[0]
    meta = _read_meta(epub)
    return DownloadResult(
        file_path=epub,
        file_format="epub",
        title=meta.get("title", "") or epub.stem,
        author=meta.get("author", ""),
        site=_site_of(url),
        source_url=url,
        num_chapters=int(meta.get("numChapters", 0) or 0),
        extra={"workdir": str(workdir), "raw_meta": meta},
    )


def _read_meta(epub: Path) -> dict:
    """Прочитать соседний <epub>.json с метаданными FanFicFare."""
    meta_file = epub.with_name(epub.name + ".json")
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}
