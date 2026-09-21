"""Обложка, выбранная человеком: загруженный файл или копия чужой обложки
(serg/tasks#1055).

Такая обложка получает `Work.cover_source = "manual"`, и автоматика (перекачка,
монитор, фоновое обновление, Calibre) её больше не заменяет. Заменить её могут
только новая ручная обложка или явное «Сгенерировать обложку».

Файл лежит под ОТДЕЛЬНЫМ именем `<sha1|w<id>>-m<md5_8>.<ext>`: имя `<sha1>.<ext>`
принадлежит встроенной/скачанной обложке, и `extract_cover` / `save_cover_bytes`
перезаписали бы ручной файл на месте, не меняя `cover_source`. Копия чужой обложки
тоже отдельный файл: `delete_work` удаляет файл обложки, а общий файл убил бы
обложку сразу двух книг.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import time
from pathlib import Path

from .config import COVERS_DIR

MAX_BYTES = 10 * 1024 * 1024
# 25 Мпикс ≈ 100 МБ несжатых: сервис живёт под MemoryMax 1G, а декодирование
# (проверка, что файл целый) держит картинку в памяти целиком.
MAX_PIXELS = 25_000_000
MIN_SIDE = 100  # меньше — иконка или логотип, а не обложка

_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_MANUAL_NAME = re.compile(r"-m[0-9a-f]{8}\.(jpg|png|webp|gif)$")


class CoverRejected(Exception):
    """Картинка не принята. `status` — HTTP-код ответа, `message` — для человека."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def inspect_image(data: bytes, *, allow_gif: bool = False) -> str:
    """Расширение файла (`.jpg`/`.png`/`.webp`) по СОДЕРЖИМОМУ или CoverRejected.

    Расширение и заголовок Content-Type присылает клиент, и верить им нельзя.
    Файл декодируется целиком: обрезанный JPEG проходит проверку заголовка и
    падает только при полном чтении — а падать должен здесь, а не при показе.
    """
    if len(data) > MAX_BYTES:
        raise CoverRejected(413, f"файл больше {MAX_BYTES // (1024 * 1024)} МБ")
    if not data:
        raise CoverRejected(422, "пустой файл")
    from PIL import Image, UnidentifiedImageError

    formats = dict(_EXT, **({"GIF": ".gif"} if allow_gif else {}))
    try:
        im = Image.open(io.BytesIO(data))
        fmt = im.format or ""
        if fmt not in formats:
            raise CoverRejected(415, "нужна картинка JPEG, PNG или WebP")
        w, h = im.size
        if w * h > MAX_PIXELS:
            raise CoverRejected(413, "картинка слишком большая по размеру в пикселях")
        if min(w, h) < MIN_SIDE:
            raise CoverRejected(422, f"картинка слишком маленькая (меньше {MIN_SIDE} px)")
        im.load()
    except CoverRejected:
        raise
    except UnidentifiedImageError:
        raise CoverRejected(415, "это не картинка JPEG, PNG или WebP") from None
    except Exception:  # noqa: BLE001 — Pillow бросает OSError/SyntaxError/ValueError/…
        raise CoverRejected(422, "картинка повреждена и не читается") from None
    return formats[fmt]


def is_manual_file(path: str | Path | None) -> bool:
    """Файл создан этим модулем и лежит в каталоге обложек — его можно удалять."""
    if not path:
        return False
    p = Path(path)
    return bool(_MANUAL_NAME.search(p.name)) and p.parent == COVERS_DIR


def _mtime(path: str | Path | None) -> int:
    try:
        return int(Path(path).stat().st_mtime) if path else 0
    except OSError:
        return 0


def write_manual_file(data: bytes, ext: str, work_id: int, sha1: str, old_path: str | None) -> Path:
    """Атомарно записать файл обложки и вернуть путь. Записи в БД здесь нет.

    `cover_v` (mtime) обязан вырасти: браузер и service worker кешируют обложку
    по URL с `?v=`, и без нового числа новая картинка не покажется.
    """
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    stem = sha1 or f"w{work_id}"
    tag = hashlib.md5(data, usedforsecurity=False).hexdigest()[:8]  # имя файла, не защита
    dest = COVERS_DIR / f"{stem}-m{tag}{ext}"
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_bytes(data)
        stamp = max(int(time.time()), _mtime(old_path) + 1)
        os.utime(tmp, (stamp, stamp))
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def discard_old(old_path: str | None, new_path: Path) -> None:
    """Убрать прежний РУЧНОЙ файл (и его превью) после успешной записи в БД.

    Чужой файл — встроенная обложка, файл другой книги — не трогаем: имя не
    ручное, значит принадлежит не нам.
    """
    if not old_path or Path(old_path) == new_path or not is_manual_file(old_path):
        return
    old = Path(old_path)
    old.unlink(missing_ok=True)
    thumbs = old.parent / "_thumbs"
    if thumbs.is_dir():
        for stale in thumbs.glob(f"{old.stem}_*"):
            stale.unlink(missing_ok=True)
