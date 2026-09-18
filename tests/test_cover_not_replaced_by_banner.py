"""Баннер сайта не должен подменять обложку книги (serg/tasks#984).

Живой случай 18.09.2026: «Вечно голодный студент 10». Докачку выиграло зеркало
readli, из его epub извлеклась картинка 122×41 — логотип сайта. Она записалась
как обложка (cover_source='embedded') поверх настоящей обложки с author.today,
а выдача /cover тем же критерием сочла её мусором и ответила 404. На странице
книги осталось пустое место.

Смысл теста: запись и показ обложки должны судить об одной и той же картинке
ОДИНАКОВО. Разъедутся — книга снова останется без обложки, и ошибки нигде не
будет.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from backend.app import services


def _png(path: Path, size: tuple[int, int], color=(40, 90, 160)) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


def test_banner_shaped_image_is_a_placeholder(tmp_path):
    """122×41 — пропорции баннера, а не книжной обложки."""
    p = _png(tmp_path / "banner.png", (122, 41))
    assert services._is_placeholder_cover(p) is True


def test_real_cover_is_not_a_placeholder(tmp_path):
    """Обычная книжная обложка проходит."""
    p = _png(tmp_path / "cover.png", (600, 900))
    assert services._is_placeholder_cover(p) is False


def test_missing_file_is_not_a_placeholder(tmp_path):
    """Файла нет — это не повод объявлять картинку мусором (и не повод падать)."""
    assert services._is_placeholder_cover(tmp_path / "нет-такого.png") is False
