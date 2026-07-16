"""Регресс-тесты v3 (2026-07-17): потеря новой главы «Вечно голодный студент 9».

Корень: _richness считал base64-обложку fb2 <binary> за текст → отставшее
searchfloor-зеркало «перевешивало» свежий author.today-epub с лишней главой;
плюс AT-скрейп брал кнопку «Купить цикл» как серию → same_book давал CONFLICT
и плодил дубль Work.
"""
import os
import tempfile

from backend.app.book_identity import same_book
from backend.app.services import _richness


def test_richness_ignores_fb2_binary():
    """base64 <binary> (обложка) НЕ должен раздувать меру полноты текста."""
    body = "<section><title>Глава первая</title><p>" + "слово " * 2000 + "</p></section>"
    binary = (
        "<binary id='cover.jpg' content-type='image/jpeg'>"
        + "A" * 600000
        + "</binary>"
    )
    fb2 = f"<?xml version='1.0'?><FictionBook>{body}{binary}</FictionBook>"
    fd, path = tempfile.mkstemp(suffix=".fb2")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(fb2)
        r = _richness(path, "fb2")
        assert r < 50000, f"base64 <binary> протёк в richness: {r}"
        assert r > 5000, f"текст книги потерян: {r}"
    finally:
        os.remove(path)


def test_same_book_at_mirror_merges_after_series_cleaned():
    """Одна книга: calibre-fb2 (серия из fb2) и AT-epub (серия очищена → UNKNOWN).
    Один автор, тот же том → same_book, дубль не создаётся."""
    a = {
        "title": "Вечно голодный студент 9",
        "author": "RedDetonator",
        "series": "Вечно голодный студент",
        "annotation": "",
    }
    b = {
        "title": "Вечно голодный студент 9",
        "author": "RedDetonator",
        "series": "",
        "annotation": "",
    }
    assert same_book(a, b) is True


def test_same_book_paywall_button_caused_conflict():
    """Демонстрация исходного дефекта: пока AT-скрейп клал 'Купить цикл' в series,
    same_book того же автора/тома давал CONFLICT → дубль. Фикс — в authortoday-скрейпе
    (кнопка покупки не читается как серия), поэтому в проде series больше не 'Купить цикл'."""
    a = {"title": "Вечно голодный студент 9", "author": "RedDetonator",
         "series": "Вечно голодный студент", "annotation": ""}
    b = {"title": "Вечно голодный студент 9", "author": "RedDetonator",
         "series": "Купить цикл", "annotation": ""}
    assert same_book(a, b) is False
