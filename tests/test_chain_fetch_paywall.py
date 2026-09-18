"""Подмена платной книги бесплатным зеркалом не должна стирать факт платности.

serg/tasks#983. Ветка author.today в `chain.fetch`, увидев `partial_paid`, сама
ищет бесплатное зеркало и возвращает ЕГО, если оно полнее. Дальше по цепочке
никто уже не знает, что часть глав продаётся: монитор видит «в файле 21, на сайте
22», считает это неудачей докачки и штрафует подписку — через пять тиков книга
выпадает из автообновления. Ровно это и случилось с «Вечно голодным студентом 10»
(на author.today 22 заголовка, 22-я платная; readli отдаёт 21 главу).

Тест идёт без сети: загрузчик author.today и поиск по зеркалам подменяются.
"""

from __future__ import annotations

import pytest

from backend.downloaders import authortoday, chain


class _Res:
    def __init__(self, extra=None, source_url: str = "", title: str = "Книга") -> None:
        self.extra = extra if extra is not None else {}
        self.source_url = source_url
        self.title = title
        self.author = "Автор"


@pytest.fixture
def _no_network(monkeypatch):
    """Платная книга на author.today и более полное бесплатное зеркало."""
    paid = _Res({"partial_paid": True}, "https://author.today/work/635146")
    free = _Res({"workdir": "/tmp/x"}, "https://readli.net/chitat-online/?b=1387674")
    monkeypatch.setattr(authortoday, "download", lambda url, creds=None: paid)
    monkeypatch.setattr(chain, "_search_free", lambda title, author="": free)
    # Зеркало «полнее» — значит именно оно и вернётся вместо платного оригинала.
    monkeypatch.setattr(chain, "_richness", lambda r: 2 if r is free else 1)
    return paid, free


def test_free_mirror_carries_the_paywall_fact(_no_network):
    paid, free = _no_network
    got = chain.fetch("https://author.today/work/635146")
    assert got is free, "вернулось не зеркало — условия теста изменились"
    assert got.extra.get("primary_partial_paid") is True, (
        "факт платности потерян: монитор оштрафует подписку за чужую коммерцию"
    )


def test_full_free_book_gets_no_flag(monkeypatch):
    """Обычная книга: платного хвоста нет — и помечать нечего."""
    plain = _Res({}, "https://author.today/work/1")
    monkeypatch.setattr(authortoday, "download", lambda url, creds=None: plain)
    got = chain.fetch("https://author.today/work/1")
    assert got is plain
    assert "primary_partial_paid" not in got.extra
