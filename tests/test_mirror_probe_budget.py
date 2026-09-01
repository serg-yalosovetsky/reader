"""Зонд зеркал не должен разорять чужие сервисы и растягивать тик.

Приёмка serg/tasks#316 показала: после снятия белого списка хостов зонд ходит на
searchfloor и author.today по ВСЕМ подпискам каждые 20 минут — три последовательных
запроса на подписку с таймаутом 90 с. При залипшем хосте одна фаза занимает
70/5 × 3 × 90 ≈ 63 минуты при тике в 20 минут, а трафик к author.today идёт через
резидентный egress (домашняя нода), то есть бан прилетает на домашний IP.

Две меры: короткий таймаут у зондов и кэш результата на 6 часов.
"""

from __future__ import annotations

import time

from backend.accounts import monitor
from backend.downloaders import searchfloor


def _task(work_id: int = 58) -> dict:
    return {
        "host": "ficbook.net",
        "work_id": work_id,
        "title": "Сломанный Меч",
        "our": {"title": "Сломанный Меч", "author": "Atlet123"},
        "at_creds": None,
    }


def test_probe_timeout_is_short_and_download_stays_patient():
    """Скачивание книги терпит 90 с, зонд мониторинга — нет."""
    assert searchfloor._PROBE_TIMEOUT <= 20
    assert searchfloor._client().timeout.read == 90
    assert searchfloor._client(searchfloor._PROBE_TIMEOUT).timeout.read == (
        searchfloor._PROBE_TIMEOUT
    )


def test_mirror_probe_result_is_cached(monkeypatch):
    """Второй тик подряд не ходит в сеть — берёт результат из кэша."""
    monitor.reset_mirror_cache()
    calls = []

    def _probe(our, at_creds=None):
        calls.append(our)
        return ("https://searchfloor.org/b/999", 70)

    monkeypatch.setattr(monitor, "_check_mirrors", _probe)

    first = monitor._at_task(_task())
    second = monitor._at_task(_task())
    third = monitor._at_task(_task())

    assert first == second == third == ("https://searchfloor.org/b/999", 70)
    assert len(calls) == 1, "зонд обязан ходить в сеть один раз на TTL"


def test_cache_expires(monkeypatch):
    monitor.reset_mirror_cache()
    calls = []
    monkeypatch.setattr(
        monitor, "_check_mirrors", lambda our, at_creds=None: calls.append(1)
    )

    monitor._at_task(_task())
    # состарим запись
    ts, res = monitor._mirror_cache[58]
    monitor._mirror_cache[58] = (ts - monitor._MIRROR_TTL_SEC - 1, res)
    monitor._at_task(_task())

    assert len(calls) == 2


def test_failure_is_not_cached(monkeypatch):
    """Сетевой сбой не должен запирать книгу без зеркал на шесть часов."""
    monitor.reset_mirror_cache()
    calls = []

    def _boom(our, at_creds=None):
        calls.append(1)
        raise RuntimeError("searchfloor не отвечает")

    monkeypatch.setattr(monitor, "_check_mirrors", _boom)

    assert monitor._at_task(_task()) is None
    assert monitor._at_task(_task()) is None
    assert len(calls) == 2, "неудачу кэшировать нельзя"


def test_negative_result_is_cached(monkeypatch):
    """«Зеркал нет» — полноценный ответ, его кэшировать НУЖНО: иначе книги без
    зеркал (а их большинство) ходят в сеть каждый тик впустую."""
    monitor.reset_mirror_cache()
    calls = []

    def _none(our, at_creds=None):
        calls.append(1)
        return None

    monkeypatch.setattr(monitor, "_check_mirrors", _none)

    assert monitor._at_task(_task()) is None
    assert monitor._at_task(_task()) is None
    assert len(calls) == 1


def test_manual_check_drops_the_cache(monkeypatch):
    """Ручная проверка = «посмотри заново», включая зеркала."""
    monitor.reset_mirror_cache()
    monitor._mirror_cache[58] = (time.time(), ("https://searchfloor.org/b/1", 5))

    class _S:
        def exec(self, *a, **kw):
            return _R()

        def add(self, *a, **kw):
            pass

        def commit(self):
            pass

    class _R:
        def all(self):
            return []

    monitor.reset_fail_counters(_S())
    assert monitor._mirror_cache == {}
