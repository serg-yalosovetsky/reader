"""Идентичность книги: тёзки не сливаются, ник/имя/транслит — сливаются."""

from __future__ import annotations

import pytest

from backend.app.book_identity import (
    CONFLICT,
    MATCH,
    author_relation,
    same_book,
    title_matches,
)

_LOREM_A = (
    "Молодой человек внезапно оказывается в теле пса и вынужден выживать "
    "в большом городе, постепенно понимая, кем он был раньше." * 3
)
_LOREM_B = (
    "Совершенно другая история про экономический триллер на Уолл-стрит "
    "и биржевые махинации главного героя." * 3
)

_CASES = [
    ("тёзка Собачья Cokol-d vs Анна Светлая",
     {"title": "Собачья Жизнь", "author": "Cokol-d"},
     {"title": "Собачья жизнь", "author": "Анна Светлая"}, False),
    ("тёзка Оракул Архин vs Денис Бобкин",
     {"title": "Оракул", "author": "Архин"},
     {"title": "Оракул", "author": "Денис Бобкин"}, False),
    ("тёзка Из Тьмы Добродел vs Дмитрий Казаков",
     {"title": "Из Тьмы", "author": "Добродел"},
     {"title": "Из тьмы", "author": "дмитрий казаков"}, False),
    ("ник вложен noslnosl",
     {"title": "Крушение сурка. Том 2", "author": "noslnosl"},
     {"title": "Крушение сурка. Том 2", "author": 'Абрамов Владимир "noslnosl"'}, True),
    ("кавычки RedDetonator",
     {"title": "Вечно голодный студент 6", "author": "RedDetonator"},
     {"title": "Вечно голодный студент 6", "author": '"RedDetonator"'}, True),
    ("тот же автор дубль Созидатель 2",
     {"title": "Созидатель 2", "author": "Cokol-d"},
     {"title": "Созидатель 2", "author": "Cokol-d"}, True),
    ("разные тома Оракул vs Оракул 2",
     {"title": "Оракул", "author": "Икс"},
     {"title": "Оракул 2", "author": "Икс"}, False),
    ("разные тома серии Созидатель 2 vs 3",
     {"title": "Созидатель 2", "author": "Cokol-d"},
     {"title": "Созидатель 3", "author": "Cokol-d"}, False),
    ("опечатка автора Уилан/Уилэн",
     {"title": "Голая экономика", "author": "Чарльз Уилан"},
     {"title": "Голая экономика", "author": "Чарлз Уилэн"}, True),
    ("транслит Сандерсон/Sanderson",
     {"title": "Обреченное королевство", "author": "Брендон Сандерсон"},
     {"title": "Обреченное королевство", "author": "Brandon Sanderson"}, True),
    ("ник vs имя без аннотации → разные",
     {"title": "Нагльфар: Иллюзия и Реальность", "author": "Морроу Винд"},
     {"title": "Нагльфар: Иллюзия и Реальность", "author": "Кузьмин Марк, Дмитрий Чильдинов"}, False),
    ("ник vs имя с похожей аннотацией → та же",
     {"title": "Нагльфар: Иллюзия и Реальность", "author": "Морроу Винд", "annotation": _LOREM_A},
     {"title": "Нагльфар: Иллюзия и Реальность", "author": "Кузьмин Марк", "annotation": _LOREM_A}, True),
    ("тёзка с разной аннотацией не сливается",
     {"title": "Оракул", "author": "Архин", "annotation": _LOREM_A},
     {"title": "Оракул", "author": "Алим Тыналин", "annotation": _LOREM_B}, False),
    ("серия совпала, номер разный → разные",
     {"title": "Путь", "author": "Икс", "series": "Хроники", "series_index": 1},
     {"title": "Путь", "author": "Икс", "series": "Хроники", "series_index": 3}, False),
]


@pytest.mark.parametrize("desc,a,b,expected", _CASES, ids=[c[0] for c in _CASES])
def test_same_book(desc, a, b, expected):
    assert same_book(a, b) is expected


def test_author_relation_basics():
    assert author_relation("Cokol-d", "Анна Светлая") == CONFLICT
    assert author_relation("noslnosl", 'Абрамов Владимир "noslnosl"') == MATCH
    assert author_relation("", "кто-то") == "unknown"


# --- текст-фолбэк: когда аннотации нет, сверяем по тексту книги ---
_TXT_A = ("Глава первая. Герой очнулся в незнакомом лесу под чёрным небом, "
          "не помня ничего о прошлом, кроме огня во снах. " * 30)
_TXT_B_SAME = _TXT_A
_TXT_C_DIFF = ("Совсем другая книга про космический корабль, экипаж которого "
               "исследует далёкую галактику и сталкивается с чужим разумом. " * 30)


def test_text_fallback_same_no_annotation():
    a = {"title": "Нагльфар", "author": "Морроу Винд"}
    b = {"title": "Нагльфар", "author": "Кузьмин Марк"}  # ник vs имя, аннотаций нет
    assert same_book(a, b) is False  # без текста — консервативно разные
    assert same_book(a, b, get_text_a=lambda: _TXT_A, get_text_b=lambda: _TXT_B_SAME) is True


def test_text_fallback_diff_text():
    a = {"title": "Оракул", "author": "Архин"}
    b = {"title": "Оракул", "author": "Денис Бобкин"}
    assert same_book(a, b, get_text_a=lambda: _TXT_A, get_text_b=lambda: _TXT_C_DIFF) is False


def test_text_fallback_unknown_author():
    a = {"title": "Книга", "author": ""}  # автор неизвестен
    b = {"title": "Книга", "author": "Некто"}
    assert same_book(a, b, get_text_a=lambda: _TXT_A, get_text_b=lambda: _TXT_B_SAME) is True


# --- поиск по ОДНОМУ названию: автора/аннотации/файла ещё нет ---
# Живой баг: «Крушение сурка. Том 2» readli находил, а _search_free отбрасывал
# КАЖДОЕ зеркало — same_book при пустом авторе не имеет ни одного сигнала-опоры
# и консервативно отвечает «разные». Опора при таком запросе — только название.


def test_title_matches_same_volume():
    assert title_matches("Крушение сурка. Том 2", "Крушение сурка. Том 2") is True


def test_title_matches_rejects_other_volume():
    # Ради чего гейт и стоит: запросили 9-й том, зеркало отдаёт 5-й.
    assert title_matches("Вечно голодный студент 9", "Вечно голодный студент 5") is False
    assert title_matches("Оракул", "Оракул 2") is False


def test_title_matches_rejects_other_book():
    assert title_matches("Крушение сурка. Том 2", "Собачья жизнь") is False


def test_same_book_still_conservative_without_author():
    # title_matches — гейт, а не замена same_book: сама same_book без опоры
    # по-прежнему отвечает «разные».
    a = {"title": "Крушение сурка. Том 2", "author": ""}
    b = {"title": "Крушение сурка. Том 2", "author": 'Абрамов Владимир "noslnosl"'}
    assert same_book(a, b) is False
