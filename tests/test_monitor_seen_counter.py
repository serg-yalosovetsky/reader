"""Почему книга переставала обновляться навсегда (serg/tasks#983).

Живой случай 18.09.2026: «Вечно голодный студент 10» (author.today/work/635146,
подписка 459). В `last_seen_chapters` осело 77 при 22 главах на сайте и 19 в
файле. Каждая проверка считала 22, сравнивала с 77 — «на сайте не больше, чем
видели» — и объявляла книгу актуальной: докачка не запускалась НИ РАЗУ, в логе
за десять дней ни строки, хотя соседние книги того же сайта качались.

Здесь два разных правила, и сломаны были оба:

1. `_set_seen` брал максимум, поэтому счётчик умел только расти. Максимум нужен
   против отставшего ЗЕРКАЛА, но свой источник в тех же единицах — авторитет.
2. «Есть ли новое» решалось сравнением с тем, что ВИДЕЛИ, а не с тем, что лежит
   в файле. Стоит счётчику сравняться с сайтом (а после починки п.1 он ровно
   так и сравнялся: 22 = 22), и недокачанная книга снова «полная» — 19 глав из
   22 остаются недокачанными навсегда.
"""

from __future__ import annotations

from backend.accounts import monitor as m


class _Mon:
    """Минимальная подписка: `_set_seen`/`_seen_for` трогают только эти поля."""

    def __init__(self, seen: int, source: str) -> None:
        self.last_seen_chapters = seen
        self.last_seen_source = source


# --- 1. Счётчик «видели» ------------------------------------------------------

def test_same_source_lowers_the_counter():
    """Тот же источник, те же единицы, число меньше — счётчик опускается."""
    mon = _Mon(77, "author.today")
    m._set_seen(mon, 22, "https://author.today/work/635146", authoritative=True)
    assert mon.last_seen_chapters == 22, (
        "завышенное «видели» осталось: книга не обновится, пока сайт не догонит"
    )
    assert mon.last_seen_source == "author.today"


def test_lagging_mirror_does_not_lower_the_counter():
    """Зеркало отстаёт — прежнее число сохраняется (ради этого и вводился max)."""
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


# --- 2. «Есть ли новое»: считаем по файлу, а не только по «видели» ------------

def test_undownloaded_book_is_an_update_even_when_counter_caught_up():
    """Тот самый случай: сайт 22, видели 22, в файле 19 — это обновление.

    Без этого правила книга, у которой счётчик однажды сравнялся с сайтом,
    остаётся неполной навсегда и молча: ни ошибки, ни строки в логе.
    """
    assert m._has_new_content(
        best_cur=22, seen=22, materialized=19, heterogeneous=False
    ) is True


def test_fully_downloaded_book_is_not_an_update():
    """Всё скачано — дёргать источник незачем."""
    assert m._has_new_content(
        best_cur=22, seen=22, materialized=22, heterogeneous=False
    ) is False


def test_more_chapters_on_site_is_still_an_update():
    """Обычный путь: на сайте больше, чем видели."""
    assert m._has_new_content(
        best_cur=23, seen=22, materialized=22, heterogeneous=False
    ) is True


def test_heterogeneous_metric_never_triggers_download():
    """Страницы readli (109) против глав в файле (25) — величины несравнимы.

    Иначе полностью скачанная книга качалась бы на каждом тике: ровно этот
    дефект уже был у «Вечно голодного студента 9» (21 гл. из 80 стр.).
    """
    assert m._has_new_content(
        best_cur=109, seen=109, materialized=25, heterogeneous=True
    ) is False


def test_unknown_file_chapters_is_not_an_update():
    """Глав в файле посчитать не смогли (цельный fb2 зеркала) — не выдумываем."""
    assert m._has_new_content(
        best_cur=22, seen=22, materialized=0, heterogeneous=False
    ) is False
