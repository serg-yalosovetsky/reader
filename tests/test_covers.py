"""Обложки: детект дженерик-баннера по форме кадра, перебор зеркал, приоритет
настоящей обложки над сгенерированной."""

from __future__ import annotations

import struct

from backend.app import covers


def _png(w: int, h: int) -> bytes:
    """Минимальный валидный PNG-заголовок с заданными размерами (для _img_size)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_body = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return sig + b"\x00\x00\x00\x0dIHDR" + ihdr_body + b"\x00\x00\x00\x00"


def test_img_size_png_portrait():
    assert covers._img_size(_png(600, 800)) == (600, 800)


def test_img_size_png_landscape():
    assert covers._img_size(_png(1200, 630)) == (1200, 630)


def test_img_size_unknown_format_is_none():
    assert covers._img_size(b"not-an-image") is None


def test_portrait_cover_is_not_generic():
    # Обложка книги портретная — настоящая, не баннер.
    assert covers.is_generic_cover(_png(600, 800), check_aspect=True) is False


def test_landscape_banner_is_generic():
    # Альбомный og:image (~1200×630) — дженерик-баннер сайта, не обложка.
    assert covers.is_generic_cover(_png(1200, 630), check_aspect=True) is True


def test_aspect_check_off_by_default():
    # Без check_aspect альбомная картинка НЕ отсекается (только md5) — чтобы не
    # ломать встроенные обложки редкой формы.
    assert covers.is_generic_cover(_png(1200, 630)) is False


def test_mirror_urls_dedup_and_search(monkeypatch):
    from backend.downloaders import authortoday, searchfloor

    monkeypatch.setattr(
        authortoday, "search_work", lambda t, a="": "https://author.today/work/42"
    )
    monkeypatch.setattr(searchfloor, "search_book", lambda t, a="": "99")

    urls = covers._mirror_urls("Название", "Автор", "https://ficbook.net/readfic/1")
    # Исходный источник всегда первый.
    assert urls[0] == "https://ficbook.net/readfic/1"
    # Зеркала на других сайтах добавлены.
    assert "https://author.today/work/42" in urls
    assert "https://searchfloor.org/b/99" in urls


def test_mirror_urls_no_title_only_source():
    # Без названия искать зеркала нечем — только исходный URL.
    urls = covers._mirror_urls("", "", "https://ficbook.net/readfic/1")
    assert urls == ["https://ficbook.net/readfic/1"]


def test_fetch_source_cover_skips_banner_takes_real(monkeypatch):
    """Первое зеркало отдаёт альбомный баннер (отсекается), второе — настоящую
    портретную обложку (берётся)."""
    from backend.downloaders import authortoday, searchfloor

    def fake_fetch(url: str):
        return _png(1200, 630) if "ficbook" in url else _png(600, 800)

    monkeypatch.setattr(covers, "fetch_cover_bytes", fake_fetch)
    monkeypatch.setattr(
        authortoday, "search_work", lambda t, a="": "https://author.today/work/42"
    )
    monkeypatch.setattr(searchfloor, "search_book", lambda t, a="": None)

    p = covers.fetch_source_cover(
        "https://ficbook.net/readfic/1", "sha_multi_test", "T", "A"
    )
    assert p is not None
    assert p.exists()


def test_fetch_source_cover_none_when_all_banners(monkeypatch):
    def fake_fetch(url: str):
        return _png(1200, 630)  # везде альбомные баннеры

    monkeypatch.setattr(covers, "fetch_cover_bytes", fake_fetch)
    from backend.downloaders import authortoday, searchfloor

    monkeypatch.setattr(authortoday, "search_work", lambda t, a="": None)
    monkeypatch.setattr(searchfloor, "search_book", lambda t, a="": None)

    p = covers.fetch_source_cover("https://ficbook.net/readfic/1", "sha_none", "T", "A")
    assert p is None
