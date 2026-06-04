"""Утилиты файлового хранилища книг: SHA-1, импорт файла в BOOKS_DIR."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .config import BOOKS_DIR

SUPPORTED_FORMATS = {".epub": "epub", ".fb2": "fb2"}


def sha1_of_file(path: Path) -> str:
    """SHA-1 файла (как у ReadEra doc_sha1) — потоково, без загрузки в память."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_format(filename: str) -> str | None:
    """epub/fb2 по расширению, иначе None."""
    return SUPPORTED_FORMATS.get(Path(filename).suffix.lower())


def import_file(src: Path, sha1: str | None = None) -> tuple[Path, str]:
    """Скопировать книгу в BOOKS_DIR под именем <sha1><ext>. Идемпотентно.

    Возвращает (итоговый путь, sha1). Имя по SHA-1 гарантирует, что один и тот же
    файл не дублируется и совпадает с идентификатором в ReadEra.
    """
    src = Path(src)
    if sha1 is None:
        sha1 = sha1_of_file(src)
    ext = src.suffix.lower()
    dest = BOOKS_DIR / f"{sha1}{ext}"
    if not dest.exists():
        BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return dest, sha1
