"""Качели «видели»: полная перекачка книги каждый второй тик (serg/tasks#1525).

Живой случай 2026-10-06: work 3508 «Чёрный Кот», подписка 454 смотрит на
author.today/work/650739, зеркало searchfloor.org/b/18580 показывает 20 глав, в
файле 21. Лог: «докачка начата … searchfloor … (на источнике 20)» в 12:57, 13:37,
14:16, 14:57, 15:38, 16:17 — через тик.

Механизм: у `last_seen_chapters` было два писателя с разными числами. Докачка
писала счёт ЛУЧШЕГО источника (зеркало, 20), а ветка «нет обновления» — счёт
СВОЕГО источника подписки (AT, 19). Следующий тик видел 20 > 19 и качал снова.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from backend.accounts import monitor
from backend.app.db.models import Monitored, Work

AT = "https://author.today/work/650739"
SF = "https://searchfloor.org/b/18580"


def _setup(session: Session, monkeypatch, *, file_chapters: int, own: int, mirror: int,
           seen: int = 20, seen_source: str = "searchfloor.org"):
    w = Work(title="Чёрный Кот", author="Автор", file_path="/nonexistent.fb2",
             file_format="fb2", chapters_count=file_chapters, source_url=AT)
    session.add(w)
    session.commit()
    session.refresh(w)
    session.add(Monitored(source_url=AT, work_id=w.id, last_seen_chapters=seen,
                          last_seen_source=seen_source))
    session.commit()

    monkeypatch.setattr(monitor, "_count_chapters_task", lambda t, *a: own)
    monkeypatch.setattr(monitor, "_at_task", lambda t: (SF, mirror))
    monkeypatch.setattr(monitor, "_file_chapters", lambda w_: file_chapters)
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)

    calls: list[tuple[str, int]] = []

    def _download(session_, mon, work_obj, best_url, best_cur):
        """Ровно то, что пишет настоящая докачка на ветке got_all."""
        calls.append((best_url, best_cur))
        mon = session_.get(Monitored, mon.id)
        mon.has_update = False
        mon.fail_count = 0
        monitor._set_seen(mon, best_cur, best_url)
        session_.add(mon)
        session_.commit()
        return {"downloaded": True, "source_used": best_url, "chapters": file_chapters}

    monkeypatch.setattr(monitor, "_download_and_write", _download)
    return w, calls


def _mon(session: Session, work_id: int) -> Monitored:
    session.expire_all()
    return session.exec(select(Monitored).where(Monitored.work_id == work_id)).one()


@pytest.mark.parametrize("file_chapters", [20, 21])
def test_no_seesaw_between_own_source_and_fuller_mirror(session, monkeypatch, file_chapters):
    """Свой источник 19, зеркало 20, файл 20 (и 21 — как у 3508): тики подряд
    ничего не качают, «видели» не скачет."""
    w, calls = _setup(session, monkeypatch, file_chapters=file_chapters, own=19, mirror=20)
    seen_after = []
    for _ in range(3):
        res = monitor.check_all(session, auto_download=True, pull_feeds=False)
        assert res["downloaded"] == 0, f"лишняя перекачка: {res['details']}"
        mon = _mon(session, w.id)
        seen_after.append((mon.last_seen_chapters, mon.last_seen_source))
    assert calls == []
    assert seen_after == [(20, "searchfloor.org")] * 3


@pytest.mark.parametrize("file_chapters", [20, 21])
def test_check_one_keeps_seen_of_the_fuller_mirror(session, monkeypatch, file_chapters):
    """Ручная проверка — второй писатель того же счётчика, правило то же."""
    w, calls = _setup(session, monkeypatch, file_chapters=file_chapters, own=19, mirror=20)
    monkeypatch.setattr(monitor, "_chapter_count", lambda url, host, creds: 19)
    monkeypatch.setattr(monitor, "_check_mirrors", lambda our, creds=None: (SF, 20))
    for _ in range(2):
        res = monitor.check_one(session, w.id)
        assert res["has_update"] is False, res
        mon = _mon(session, w.id)
        assert (mon.last_seen_chapters, mon.last_seen_source) == (20, "searchfloor.org")
    assert calls == []


def test_real_new_chapter_on_mirror_is_still_downloaded(session, monkeypatch):
    """Счёт секций в файле на единицу больше, чем глав на зеркале (21 против 20 у
    3508), поэтому решение «по файлу» пропустило бы 21-ю главу зеркала. Решение по
    «видели» её ловит."""
    w, calls = _setup(session, monkeypatch, file_chapters=21, own=19, mirror=21)
    res = monitor.check_all(session, auto_download=True, pull_feeds=False)
    assert calls == [(SF, 21)]
    assert res["downloaded"] == 1


def test_inflated_seen_is_still_lowered(session, monkeypatch):
    """serg/tasks#983 не откатывается: завышенное «видели» опускается до
    счёта проверки, иначе книга глохнет навсегда."""
    w, calls = _setup(session, monkeypatch, file_chapters=22, own=22, mirror=0,
                      seen=77, seen_source="author.today")
    monkeypatch.setattr(monitor, "_at_task", lambda t: None)
    monitor.check_all(session, auto_download=True, pull_feeds=False)
    mon = _mon(session, w.id)
    assert mon.last_seen_chapters == 22
    assert calls == []
