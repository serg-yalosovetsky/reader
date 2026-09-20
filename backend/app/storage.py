"""Утилиты файлового хранилища книг: SHA-1, импорт файла в BOOKS_DIR."""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import zipfile
from pathlib import Path

from .config import BOOKS_DIR

SUPPORTED_FORMATS = {".epub": "epub", ".fb2": "fb2", ".pdf": "pdf"}


def sha1_of_file(path: Path) -> str:
    """SHA-1 файла (как у ReadEra doc_sha1) — потоково, без загрузки в память."""
    h = hashlib.sha1(usedforsecurity=False)  # идентичность файла, не защита
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
        # Атомарно: копия в .part и rename. Оборванная прямая копия осталась бы под
        # итоговым sha1-именем, и следующий импорт увидел бы «уже есть» — битая книга
        # навсегда (задание скачивания теперь возобновляется после рестарта, #892).
        tmp = dest.with_name(dest.name + ".part")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dest)
        except BaseException:
            # Недописанный .part убираем; если и это не вышло — ошибка копирования
            # важнее, она уходит выше как есть, а мусорный .part не выглядит книгой.
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
    return dest, sha1


# Запас прочности против zip-бомбы: больше этого из архива не разворачиваем.
# Считаем фактически записанные байты, а не info.file_size: заголовок архива
# пишет тот же, кто прислал архив, и верить ему нельзя.
MAX_ZIP_UNPACKED = 512 * 1024 * 1024


def extract_books_from_zip(
    zip_path: Path, out_dir: Path, max_books: int = 200
) -> list[Path]:
    """Развернуть из zip все поддерживаемые книги в out_dir.

    Возвращает пути распакованных файлов в порядке появления в архиве.
    Имена внутри архива недоверенные, поэтому берём только basename: ни
    "../", ни абсолютный путь за пределы out_dir не выведут (zip-slip).
    Каждая книга кладётся в свой подкаталог — одинаковые имена в разных
    папках архива не затирают друг друга, а имя файла остаётся исходным
    (из него потом берётся заголовок книги).
    """
    out: list[Path] = []
    total = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir() or len(out) >= max_books:
                continue
            name = Path(info.filename.replace("\\", "/")).name
            if not name or not detect_format(name):
                continue
            sub = out_dir / f"{len(out):04d}"
            sub.mkdir(parents=True, exist_ok=True)
            dest = sub / name
            with z.open(info) as src, open(dest, "wb") as dst:
                while chunk := src.read(1 << 20):
                    total += len(chunk)
                    if total > MAX_ZIP_UNPACKED:
                        raise ValueError(
                            "архив разворачивается больше чем в 512 МБ — не принимаю"
                        )
                    dst.write(chunk)
            out.append(dest)
    return out
