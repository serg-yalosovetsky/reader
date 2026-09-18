"""Счётчик «видели глав» умел только расти — и книга переставала обновляться.

Живой случай (serg/tasks#983, 18.09.2026): «Вечно голодный студент 10»
(author.today/work/635146, подписка 459). В `last_seen_chapters` осело 77 при
`last_seen_source = "author.today"`, хотя на сайте 22 главы, а у нас скачано 19.
Каждая проверка честно считала 22, сравнивала с 77 — «на сайте не больше, чем
видели» — и объявляла книгу актуальной: `has_update=false`, докачка не
запускалась ВООБЩЕ. В логах за десять дней нет ни одной строки про эту книгу,
хотя соседние книги author.today докачивались в те же тики.

Причина в `_set_seen`: `max(_seen_for(...), value)`. Максимум защищает от
отставшего ЗЕРКАЛА (у него глав меньше, и без max подписка забыла бы, сколько
их на самом деле). Но тот же самый источник в тех же единицах — авторитет: если
author.today говорит «22», значит 22, и прежние 77 были ошибкой, от которой
книга молча умирает.
"""

from __future__ import annotations

from backend.accounts import monitor as m


class _Mon:
    """Минимальная подписка: `_set_seen`/`_seen_for` трогают только эти поля."""

    def __init__(self, seen: int, source: str) -> None:
        self.last_seen_chapters = seen
        self.last_seen_source = source


def test_same_source_lowers_the_counter():
    """Тот же источник, те же единицы, число меньше — счётчик опускается."""
    mon = _Mon(77, "author.today")
    m._set_seen(mon, 22, "https://author.today/work/635146", authoritative=True)
    assert mon.last_seen_chapters == 22, (
        "завышенное «видели» осталось: книга не обновится, пока сайт не догонит"
    )
    assert mon.last_seen_source == "author.today"


def test_lowered_counter_makes_the_update_visible_again():
    """Ради чего всё: после честного счёта «на сайте» снова больше, чем у нас.

    19 глав в файле, 22 на сайте — обновление обязано быть видно. При залипших
    77 сравнение `22 > 77` ложно, и новые главы не берутся никогда.
    """
    mon = _Mon(77, "author.today")
    url = "https://author.today/work/635146"
    m._set_seen(mon, 22, url, authoritative=True)
    assert m._seen_for(mon, url) == 22
    assert 22 > m._seen_for(mon, url) - 3  # 19 глав в файле < 22 на сайте


def test_lagging_mirror_does_not_lower_the_counter():
    """Зеркало отстаёт — прежнее число сохраняется (ради этого и вводился max).

    Подписку могут перенацелить на searchfloor, где глав меньше: понизить
    счётчик по нему значило бы «забыть» реальный объём книги.
    """
    mon = _Mon(25, "author.today")
    m._set_seen(mon, 12, "https://searchfloor.org/b/25306")
    assert mon.last_seen_chapters == 25


def test_growth_still_works():
    """Обычный рост числа глав записывается как раньше."""
    mon = _Mon(19, "author.today")
    m._set_seen(mon, 22, "https://author.today/work/635146")
    assert mon.last_seen_chapters == 22


def test_units_still_do_not_mix():
    """readli считает СТРАНИЦЫ: они не сравниваются с главами и не понижают их."""
    mon = _Mon(25, "author.today")
    url = "https://readli.net/chitat-online/?b=1367591"
    assert m._seen_for(mon, url) == 0  # база другого класса — сравнивать нечего
    m._set_seen(mon, 109, url, authoritative=True)
    assert mon.last_seen_chapters == 109
    assert mon.last_seen_source == "readli.net"
