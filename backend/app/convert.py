"""Конвертация книг с фиксированной вёрсткой (PDF) в EPUB через calibre.

Зачем: PDF — это макет страницы, а не текст. foliate-js рендерит его постранично
через pdf.js: шрифт не увеличивается, поля не настраиваются, тема не применяется,
TTS и подсветки не работают, на телефоне читать нечем. EPUB даёт перетекающий
текст — всё остальное в читалке уже умеет работать с ним.

Конвертер — `ebook-convert` из calibre (стоит на VPS системно, тот же пакет, что
и pdftoppm-обложки). Результат кладём рядом, ОРИГИНАЛ НЕ ТРОГАЕМ: конвертация
неточная (колонтитулы, разбиение), поэтому «читать оригинал» всегда доступно.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

log = logging.getLogger("reader.convert")

# Форматы, которые имеет смысл конвертировать: фиксированная вёрстка либо
# нечитаемое во foliate. EPUB/FB2 читалка рендерит сама и лучше.
CONVERTIBLE = {"pdf", "djvu", "mobi", "azw3", "doc", "docx", "rtf"}


def available() -> bool:
    """Есть ли ebook-convert в системе."""
    return bool(shutil.which(config.EBOOK_CONVERT_BIN))


def is_convertible(fmt: str) -> bool:
    return (fmt or "").lower() in CONVERTIBLE


def target_path(sha1: str) -> Path:
    """Куда кладём EPUB-версию книги (имя по sha1 оригинала — идемпотентно)."""
    return config.CONVERTED_DIR / f"{sha1}.epub"


def _args(src: Path, dest: Path, *, title: str = "", author: str = "") -> list[str]:
    """argv для ebook-convert.

    --enable-heuristics + --unwrap-factor: PDF ломает абзац на строки, эвристики
    сшивают их обратно (без этого текст читается как стихи).
    --no-default-epub-cover: обложку книги читалка ведёт сама (covers.py), пустая
    титульная страница от calibre только мешает.
    """
    args = [
        config.EBOOK_CONVERT_BIN,
        str(src),
        str(dest),
        "--enable-heuristics",
        "--unwrap-factor",
        "0.45",
        "--no-default-epub-cover",
        "--output-profile",
        "tablet",
    ]
    if title:
        args += ["--title", title]
    if author:
        args += ["--authors", author]
    return args


def convert_to_epub(
    src: Path | str,
    sha1: str,
    *,
    title: str = "",
    author: str = "",
    force: bool = False,
) -> Path:
    """Сконвертировать книгу в EPUB и вернуть путь к нему.

    Идемпотентно: готовый файл переиспользуется, если не force. Пишем во временный
    файл и переименовываем — иначе оборванная конвертация оставляет битый EPUB,
    который потом молча отдаётся читалке.

    Бросает RuntimeError с текстом ошибки, если конвертация не удалась.
    """
    src = Path(src)
    if not src.exists():
        raise RuntimeError(f"исходного файла нет: {src}")
    if not available():
        raise RuntimeError(
            f"конвертер {config.EBOOK_CONVERT_BIN} не найден (нужен пакет calibre)"
        )

    dest = target_path(sha1)
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(config.TMP_DIR)) as tmp:
        tmp_out = Path(tmp) / "out.epub"
        args = _args(src, tmp_out, title=title, author=author)
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=config.CONVERT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"конвертация не уложилась в {config.CONVERT_TIMEOUT_SEC}с"
            ) from e
        if proc.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            raise RuntimeError("ebook-convert: " + " | ".join(tail or ["пустой вывод"]))
        shutil.move(str(tmp_out), str(dest))

    log.info(
        "Сконвертировано в EPUB: %s → %s (%.1f МБ)",
        src.name,
        dest.name,
        dest.stat().st_size / 1048576,
    )
    return dest
