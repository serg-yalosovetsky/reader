"""Свой источник подписки не ответил на счёт глав (serg/tasks#1853).

Живой случай 2026-10-14: резидентный egress (домашняя нода) лежал сутки, счёт глав
author.today возвращал None у всех 30 подписок — «счёт глав без ответа 38» каждый
тик. Подписка в этом случае молча пропускалась: найденное зеркало выбрасывалось,
last_error не писался. «Вечно голодный студент 11» (AT 666240, на searchfloor
4 главы) обновлялся только ручной кнопкой.
"""

from __future__ import annotations

from sqlmodel import Session, select

from backend.accounts import monitor
from backend.app.db.models import Monitored, Work

AT = "https://author.today/work/666240"
SF = "https://searchfloor.org/b/27598"


def _setup(session: Session, monkeypatch, *, mirror):
    w = Work(title="Вечно голодный студент 11", author="Автор", file_path="/nonexistent.fb2",
             file_format="fb2", chapters_count=3, source_url=AT)
    session.add(w)
    session.commit()
    session.refresh(w)
    session.add(Monitored(source_url=AT, work_id=w.id, last_seen_chapters=3,
                          last_seen_source="searchfloor.org"))
    session.commit()

    monkeypatch.setattr(monitor, "_count_chapters_task", lambda t, *a: None)
    monkeypatch.setattr(monitor, "_at_task", lambda t: mirror)
    monkeypatch.setattr(monitor, "_file_chapters", lambda w_: 3)
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)

    calls: list[tuple[str, int]] = []

    def _download(session_, mon, work_obj, best_url, best_cur, **_kw):
        calls.append((best_url, best_cur))
        mon = session_.get(Monitored, mon.id)
        mon.has_update = False
        mon.fail_count = 0
        mon.last_error = None
        monitor._set_seen(mon, best_cur, best_url)
        session_.add(mon)
        session_.commit()
        return {"downloaded": True, "source_used": best_url, "chapters": best_cur}

    monkeypatch.setattr(monitor, "_download_and_write", _download)
    return w, calls


def _mon(session: Session, work_id: int) -> Monitored:
    session.expire_all()
    return session.exec(select(Monitored).where(Monitored.work_id == work_id)).one()


def test_new_chapter_on_mirror_downloaded_when_own_source_silent(session, monkeypatch):
    w, calls = _setup(session, monkeypatch, mirror=(SF, 4))
    res = monitor.check_all(session, auto_download=True, pull_feeds=False)
    assert calls == [(SF, 4)]
    assert res["downloaded"] == 1


def test_mirror_without_new_chapters_does_not_download(session, monkeypatch):
    w, calls = _setup(session, monkeypatch, mirror=(SF, 3))
    monitor.check_all(session, auto_download=True, pull_feeds=False)
    assert calls == []
    assert _mon(session, w.id).fail_count == 0


def test_silent_source_without_mirror_is_visible_not_penalized(session, monkeypatch):
    w, calls = _setup(session, monkeypatch, mirror=None)
    monitor.check_all(session, auto_download=True, pull_feeds=False)
    mon = _mon(session, w.id)
    assert calls == []
    assert mon.last_error and mon.last_error.startswith(monitor._NO_ANSWER)
    assert mon.fail_count == 0

    # Источник ожил — ошибка снимается сама.
    monkeypatch.setattr(monitor, "_count_chapters_task", lambda t, *a: 3)
    monitor.check_all(session, auto_download=True, pull_feeds=False)
    assert _mon(session, w.id).last_error is None


def test_silent_source_keeps_foreign_error(session, monkeypatch):
    w, _ = _setup(session, monkeypatch, mirror=None)
    mon = _mon(session, w.id)
    mon.last_error = "доступно 20 гл. из 22 — дальше платно"
    session.add(mon)
    session.commit()
    monitor.check_all(session, auto_download=True, pull_feeds=False)
    assert _mon(session, w.id).last_error == "доступно 20 гл. из 22 — дальше платно"
