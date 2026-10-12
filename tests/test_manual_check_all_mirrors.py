"""Ручная проверка книги перекачивает со ВСЕХ зеркал, а не верит счётчикам.

Живой случай — «Вечно голодный студент 10» (2026-10-12): на author.today 30 глав,
с 29-й платно, «видели» 30, в файле 28. Полная копия лежала на readli, но кнопка
«Проверить обновления» отвечала «актуально»: `_has_new_content` честно говорил,
что недостача объяснена платным хвостом, и докачка не запускалась вовсе.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import Session

from backend.accounts import monitor
from backend.app.db.models import Monitored, Work
from backend.downloaders import searchfloor
from backend.downloaders.base import DownloaderError

AT = "https://author.today/work/635146"


def _setup(session: Session, monkeypatch):
    w = Work(title="Вечно голодный студент 10", author="RedDetonator",
             file_path="/nonexistent.epub", file_format="epub", source_url=AT)
    session.add(w)
    session.commit()
    session.refresh(w)
    session.add(Monitored(source_url=AT, work_id=w.id, last_seen_chapters=30,
                          last_seen_source="author.today", paid_tail_seen=30))
    session.commit()
    monkeypatch.setattr(monitor, "_chapter_count", lambda *a, **k: 30)
    monkeypatch.setattr(monitor, "_mirror_eligible", lambda *a: False)
    monkeypatch.setattr(monitor, "_file_chapters", lambda w_: 28)
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)
    calls: list[dict] = []

    def _dl(session_, mon, work_obj, best_url, best_cur, **kw):
        calls.append(kw)
        return {"downloaded": True, "source_used": best_url, "chapters": 30}

    monkeypatch.setattr(monitor, "_download_and_write", _dl)
    return w, calls


def test_auto_check_trusts_paid_tail(session, monkeypatch):
    """Плановый тик по-прежнему не качает: платный хвост объясняет недостачу."""
    w, calls = _setup(session, monkeypatch)
    res = monitor.check_one(session, w.id)
    assert calls == []
    assert res["has_update"] is False


def test_manual_check_refetches_all_mirrors(session, monkeypatch):
    w, calls = _setup(session, monkeypatch)
    res = monitor.check_one(session, w.id, force=True)
    assert calls == [{"keep_if_worse": True}]
    assert res["refetched"] is True
    assert res["chapters_before"] == 28 and res["chapters"] == 30


def test_manual_endpoint_forces_by_default(monkeypatch):
    from backend.app.routers import accounts

    seen = {}
    monkeypatch.setattr(accounts.monitor, "check_one",
                        lambda s, wid, force=False: seen.setdefault("force", force) or {})
    accounts.check_one_now(1, session=None)
    assert seen["force"] is True


@pytest.mark.parametrize(
    "new_rich,new_ch,poorer",
    [(900, 28, True),    # меньше текста, глав не больше — обрезок, не берём
     (900, 30, False),   # глав больше — берём, даже если текста чуть меньше
     (1000, 28, False)], # столько же — не хуже
)
def test_poorer_than_current(monkeypatch, new_rich, new_ch, poorer):
    cur_path = "/cur.epub"
    monkeypatch.setattr(monitor.chain, "_richness",
                        lambda r: 1000 if r.file_path == cur_path else new_rich)
    monkeypatch.setattr(monitor.chain, "_chapters",
                        lambda r: 28 if r.file_path == cur_path else new_ch)
    res = SimpleNamespace(file_path="/new.epub", file_format="epub")
    desc = {"file_path": cur_path, "file_format": "epub"}
    assert monitor._poorer_than_current(res, desc) is poorer


# --- searchfloor: временный 401 при пересборке файла ---


def _sf(monkeypatch, codes):
    seq = iter(codes)

    def handler(req):
        if "/book/" in req.url.path:
            c = next(seq)
            return httpx.Response(c, content=b"PKfake" if c == 200 else b"")
        return httpx.Response(200, text="<html><title>T</title></html>")

    real = httpx.Client
    monkeypatch.setattr(searchfloor.httpx, "Client",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(searchfloor.time, "sleep", lambda *_: None)
    monkeypatch.setattr(searchfloor, "_book_meta", lambda bid: ("T", "A"))


def test_searchfloor_retries_transient_401(monkeypatch):
    _sf(monkeypatch, [401, 200])
    res = searchfloor._download_book("1", "https://searchfloor.org/b/1")
    assert res.file_path.exists()


def test_searchfloor_persistent_401_still_fails(monkeypatch):
    _sf(monkeypatch, [401, 401, 401])
    with pytest.raises(DownloaderError, match="вернуло 401"):
        searchfloor._download_book("1", "https://searchfloor.org/b/1")


def test_searchfloor_404_is_not_retried(monkeypatch):
    _sf(monkeypatch, [404])
    with pytest.raises(DownloaderError, match="вернуло 404"):
        searchfloor._download_book("1", "https://searchfloor.org/b/1")
