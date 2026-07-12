"""Идентичность книги: тёзки не сливаются, ник/имя/транслит — сливаются."""

from __future__ import annotations

import pytest

from backend.app.book_identity import CONFLICT, MATCH, author_relation, same_book

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
