"""Платный хвост первичного источника не должен теряться при выборе зеркала.

serg/tasks#983, «Вечно голодный студент 10». На author.today (источник подписки)
22 заголовка, последняя глава продаётся: адаптер отдаёт доступные главы и
выставляет `partial_paid`. Но `fetch_fullest` берёт САМЫЙ ПОЛНЫЙ вариант, а это
readli — 21 глава и пустой `extra`. Монитор получал результат без единого следа
платности, считал «докачано 21 из 22» неудачей и растил счётчик: пять тиков — и
подписка выпадала из автообновления.

Флаг переносится ОТДЕЛЬНЫМ ключом `primary_partial_paid`: файл победителя не
«частично платный», и смешивать эти два факта нельзя.
"""

from __future__ import annotations

from backend.downloaders import chain


class _Res:
    """Минимальный результат докачки: переносу важен только `extra`."""

    def __init__(self, extra=None, source_url: str = "") -> None:
        self.extra = extra
        self.source_url = source_url


def test_paywall_of_primary_source_reaches_the_winner():
    primary = _Res({"partial_paid": True}, "https://author.today/work/635146")
    winner = _Res({"workdir": "/tmp/x"}, "https://readli.net/chitat-online/?b=1387674")
    assert chain._carry_paywall(primary, winner) is winner
    assert winner.extra.get("primary_partial_paid") is True, (
        "монитор не узнает, что недостача объясняется платными главами, "
        "и оштрафует подписку"
    )


def test_ordinary_book_gets_no_flag():
    """Первичный источник отдал всё — переносить нечего."""
    primary = _Res({}, "https://author.today/work/1")
    winner = _Res({"workdir": "/tmp/x"})
    chain._carry_paywall(primary, winner)
    assert "primary_partial_paid" not in winner.extra


def test_winner_is_the_primary_source():
    """Победил сам первичный источник — у него уже есть свой `partial_paid`."""
    res = _Res({"partial_paid": True})
    assert chain._carry_paywall(res, res) is res
    assert "primary_partial_paid" not in res.extra


def test_missing_primary_or_winner_is_safe():
    """Первичный источник не отдал книгу вовсе — перенос молча пропускается."""
    winner = _Res({})
    assert chain._carry_paywall(None, winner) is winner
    assert chain._carry_paywall(_Res({"partial_paid": True}), None) is None


def test_winner_without_dict_extra_does_not_crash():
    """Странный результат без словаря `extra` не должен ронять докачку."""
    primary = _Res({"partial_paid": True})
    winner = _Res(None)
    assert chain._carry_paywall(primary, winner) is winner
