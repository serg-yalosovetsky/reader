"""serg/tasks#319 — «бесплатно» ≠ «скачивается», и «Купить цикл» ≠ «книга платная».

Детектор платности author.today судил по подстрокам со ВСЕЙ страницы («Купить»,
«руб.») и ошибался в обе стороны. Замер по 31 книге библиотеки (2026-09-01):
  • «Вечно голодный студент 10», work/475012 — кнопка «Читать книгу», но на
    странице есть «Купить цикл» (покупка СЕРИИ) → считались платными;
  • «Крушение сурка. Том 1» — отдаёт только «Читать фрагмент», слова «Купить»
    на странице нет → считалась бесплатной.

Надёжный признак — кнопка чтения самой книги: «Читать книгу» (btn-success) против
«Читать фрагмент» (btn-primary). Фрагменты HTML ниже сняты с живых страниц.
"""

from __future__ import annotations

import pytest

from backend.downloaders import chain
from backend.downloaders.authortoday import _is_free
from backend.downloaders.base import DownloaderError, PaidContentError

# --- фрагменты живых страниц author.today ---

FREE_WITH_SERIES_BUY = """
<a href="/reader/622390" class="btn btn-block btn-success btn-read-book mt-lg">
  <i class="icon-2-book-open icon-fw"></i> Читать книгу </a>
<a class="btn btn-default btn-block"><i class="icon-2-download"></i> Скачать</a>
<a data-bind="click: showAuthModal" class="btn btn-buy-series mt-lg">
  <i class="icon-2-books-stack icon-fw"></i> Купить цикл </a>
"""

PAID_FRAGMENT_ONLY = """
<a href="/reader/622396" class="btn btn-block btn-primary btn-read-book mt-lg">
  <i class="icon-2-book-open icon-fw"></i> Читать фрагмент </a>
<a data-bind="click: showAuthModal" class="btn btn-buy-series mt-lg">
  <i class="icon-2-books-stack icon-fw"></i> Купить цикл </a>
"""

# «Крушение сурка. Том 1»: только фрагмент, слова «Купить» на странице нет
PAID_WITHOUT_BUY_WORD = """
<a href="/reader/185437" class="btn btn-block btn-primary btn-read-book mt-lg">
  Читать фрагмент </a>
<a class="btn btn-default btn-reward">Награды 3</a>
"""

FREE_PLAIN = """
<a href="/reader/381440" class="btn btn-block btn-success btn-read-book mt-lg">
  Читать книгу </a>
<a class="btn btn-default btn-block">Скачать</a>
<a class="btn btn-default btn-block" href="/audiobook/418129">Аудиокнига</a>
"""


def test_series_purchase_does_not_make_the_book_paid():
    """«Купить цикл» — про СЕРИЮ. У книги своя кнопка «Читать книгу»."""
    assert _is_free(FREE_WITH_SERIES_BUY) is True


def test_fragment_only_book_is_paid():
    """«Читать фрагмент» вместо «Читать книгу» — текст целиком не отдаётся."""
    assert _is_free(PAID_FRAGMENT_ONLY) is False


def test_paid_book_without_the_word_buy_is_still_paid():
    """Отсутствие слова «Купить» бесплатности НЕ доказывает."""
    assert _is_free(PAID_WITHOUT_BUY_WORD) is False


def test_plain_free_book():
    assert _is_free(FREE_PLAIN) is True


def test_fallback_heuristic_ignores_series_purchase():
    """Кнопки чтения нет (редизайн) — фоллбэк не должен ловиться на «Купить цикл»."""
    no_read_button = """
    <a data-bind="click: showAuthModal" class="btn btn-buy-series mt-lg">
      Купить цикл </a>
    <div class="annotation">текст аннотации</div>
    """
    assert _is_free(no_read_button) is True


# --- причина недоступности: 18+ ≠ платно ---


def test_paid_content_error_carries_reason():
    assert PaidContentError("Книга").reason == "paid"
    assert PaidContentError("Книга", reason="adult").reason == "adult"
    assert "18+" in str(PaidContentError("Книга", reason="adult"))


def test_adult_gate_is_not_reported_as_paid(monkeypatch):
    """Книга 18+ без зеркал: сообщение говорит про вход, а не про деньги.

    Живой случай: work 46 и work 58 на сайте открываются кнопкой «Читать книгу»
    (то есть бесплатны), но AT отвечает `unadulted` на каждую главу, потому что
    вход в аккаунт не проходит. Текст «Книга платная на author.today» тут — ложь,
    которая уводит к покупке вместо починки учётки.
    """
    monkeypatch.setattr(chain, "_search_free", lambda t, a="": None)

    with pytest.raises(DownloaderError) as ei:
        chain._fallback_free("Сломанный Меч", "Atlet123", "adult")
    msg = str(ei.value)
    assert "18+" in msg
    assert "учётку author.today" in msg
    assert "платная" not in msg.lower()


def test_paid_reason_still_says_paid(monkeypatch):
    monkeypatch.setattr(chain, "_search_free", lambda t, a="": None)

    with pytest.raises(DownloaderError) as ei:
        chain._fallback_free("Кровавые закаты предгорий", "", "paid")
    assert "платная на author.today" in str(ei.value)


def test_default_reason_is_paid(monkeypatch):
    """Старые вызовы без reason ведут себя как раньше."""
    monkeypatch.setattr(chain, "_search_free", lambda t, a="": None)

    with pytest.raises(DownloaderError) as ei:
        chain._fallback_free("Книга", "Автор")
    assert "платная на author.today" in str(ei.value)
