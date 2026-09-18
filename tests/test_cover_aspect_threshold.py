"""Порог «баннер против альбомной обложки» (serg/tasks#997).

Живой случай 18.09.2026: в консоли библиотеки висели 404 обложек книг 2089 и
2337. Файлы на диске БЫЛИ и оказались настоящими картинками — 1200×750 и
737×500. Их браковала проверка формы кадра: порог стоял на 1.15, то есть
«хоть сколько-нибудь шире, чем выше — баннер».

Замер по всей библиотеке (1454 книги) показал 120 книг с годным файлом, но без
картинки на экране. Из них 95 — настоящие заглушки по md5 (там 404 правильный),
а 25 забракованы ТОЛЬКО за форму кадра, и это две разные группы:

  баннеры        122×41 (2.98), 160×83 (1.93), 1200×630 (1.90), 1404×517 (2.72)
  обложки        1305×921 (1.42), 1650×1115 (1.48), 1200×750 (1.60),
                 1536×1024 (1.50), 737×500 (1.47), 600×382 (1.57)

Между группами чистый разрыв: 1.60 против 1.90. Порог ставится между ними.

Почему НЕ «отключить проверку формы для embedded», хотя это напрашивается и
прямо предложено комментарием в covers.py: картинка 122×41 из serg/tasks#984
записана ровно как `embedded` — отключение вернуло бы тот дефект. Единый
критерий для записи и показа обязателен: разъедутся — книга снова останется без
обложки, никому не пожаловавшись.
"""

from __future__ import annotations

import struct

import pytest

from backend.app import covers


def _png(w: int, h: int) -> bytes:
    """Минимальный валидный PNG-заголовок с заданными размерами."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_body = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return sig + b"\x00\x00\x00\x0dIHDR" + ihdr_body + b"\x00\x00\x00\x00"


# Размеры взяты из ЖИВОЙ библиотеки — это не выдуманные случаи.
REAL_COVERS = [
    (1305, 921, "Хакинг с самого начала (work 477)"),
    (840, 631, "Digital Design and Computer Architecture (work 3231)"),
    (737, 500, "Путь к Победе (work 2337)"),
    (1650, 1115, "Профессиональная разработка на Python (work 777)"),
    (1303, 813, "The Hacker Crackdown (work 509)"),
    (1536, 1024, "Госпиталь Конохи (work 2132)"),
    (1200, 750, "Ergo Proxy — разбор по эпизодам (work 2089)"),
    (600, 382, "Ergo Proxy — разборы из LiveJournal (work 2090)"),
    (1577, 1113, "Linear Algebra and Learning from Data (work 998)"),
]

REAL_BANNERS = [
    (122, 41, "логотип readli — тот самый из #984"),
    (160, 83, "мелкая растяжка (work 351, 1153)"),
    (1200, 630, "классический og:image (work 2087)"),
    (1404, 517, "широкая растяжка (work 1480)"),
]


@pytest.mark.parametrize("w,h,what", REAL_COVERS)
def test_landscape_book_cover_is_not_a_banner(w: int, h: int, what: str):
    """Альбомная, но настоящая обложка обязана доходить до экрана."""
    assert covers.is_generic_cover(_png(w, h), check_aspect=True) is False, what


@pytest.mark.parametrize("w,h,what", REAL_BANNERS)
def test_real_banner_is_still_rejected(w: int, h: int, what: str):
    """Ослабление порога НЕ должно пропустить настоящие баннеры."""
    assert covers.is_generic_cover(_png(w, h), check_aspect=True) is True, what


def test_portrait_cover_still_passes():
    """Обычная книжная обложка — вне подозрений (как и было)."""
    assert covers.is_generic_cover(_png(600, 800), check_aspect=True) is False


def test_square_cover_passes():
    """Квадрат — не баннер: так выглядят обложки аудиокниг и части сборников."""
    assert covers.is_generic_cover(_png(800, 800), check_aspect=True) is False


def test_threshold_sits_between_the_two_groups():
    """Порог именно в разрыве между живыми группами: 1.60 проходит, 1.90 — нет."""
    assert covers._BANNER_MIN_ASPECT > 1.60
    assert covers._BANNER_MIN_ASPECT < 1.90
