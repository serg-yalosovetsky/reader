"""Обложка книги переживает любую перекачку файла (serg/tasks#984).

Правило, сформулированное владельцем 18.09.2026: «обложка сохраняется, потом
скачивается книга — если там нету другой обложки, то используется первая».

То есть у обложки своя жизнь: её не отбирают ни зеркало без картинки, ни файл, из
которого извлёкся баннер сайта. Заменяет прежнюю обложку ТОЛЬКО настоящая новая.

Живой случай, из которого правило родилось: докачку «Вечно голодного студента 10»
выиграло зеркало readli, из его epub извлеклась картинка 122×41 (логотип сайта),
и она записалась поверх обложки с author.today. Показ такую картинку отбраковывает
— на странице книги осталось пустое место.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from backend.app import services


class _Work:
    """Книга: правилу важны только эти два поля."""

    def __init__(self, cover_path: str = "", cover_source: str = "") -> None:
        self.cover_path = cover_path
        self.cover_source = cover_source


def _png(path: Path, size: tuple[int, int]) -> Path:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 90, 160)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


def test_new_file_without_cover_keeps_the_old_one(tmp_path):
    """Главный случай правила: новой обложки нет — остаётся первая."""
    work = _Work("/covers/старая.jpg", "source")
    assert services._pick_cover(None, "", work) is None, (
        "вернулась замена там, где менять нечем — прежняя обложка будет затёрта"
    )


def test_banner_does_not_replace_a_real_cover(tmp_path):
    """Баннер сайта из файла зеркала — не обложка."""
    banner = _png(tmp_path / "banner.png", (122, 41))
    work = _Work("/covers/старая.jpg", "source")
    assert services._pick_cover(banner, "embedded", work) is None


def test_real_new_cover_wins(tmp_path):
    """Настоящая новая обложка заменяет прежнюю — так и задумано."""
    fresh = _png(tmp_path / "cover.png", (600, 900))
    work = _Work("/covers/старая.jpg", "source")
    assert services._pick_cover(fresh, "embedded", work) == (str(fresh), "embedded")


def test_first_cover_for_a_book_without_any(tmp_path):
    """У книги обложки ещё не было — берём любую годную."""
    fresh = _png(tmp_path / "cover.png", (600, 900))
    assert services._pick_cover(fresh, "source", _Work()) == (str(fresh), "source")


def test_banner_is_better_than_nothing_when_book_has_no_cover(tmp_path):
    """А вот когда у книги нет вообще ничего, баннер всё равно не годится:
    показ его отбракует, и книга останется без картинки, зато с мусором в базе."""
    banner = _png(tmp_path / "banner.png", (122, 41))
    assert services._pick_cover(banner, "embedded", _Work()) is None
