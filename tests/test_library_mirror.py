"""Ручное зеркало книги (serg/tasks#1853).

Живой случай: «Культивируй. Умри. Начни сначала.» переехала на ficbook под
новым id, старая страница отдавала 404, подписку сняли чёрным списком — и книга
перестала обновляться, а «Оригинал» вёл на мёртвый адрес.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from backend.app.db.models import Monitored, Work
from backend.app.db.session import engine

OLD = "https://ficbook.net/readfic/019fc393-fe5a-73a5-8858-c60d91fc3005"
NEW = "https://ficbook.net/readfic/01a051eb-a3c8-7a4c-b040-c0501125af2e"


_created: list[int] = []


@pytest.fixture(autouse=True)
def _cleanup(client):
    """База у тестов общая: книги с source_url отсюда попадали в чужие проверки
    (фоновое обновление обложек в test_manual_cover)."""
    yield
    while _created:
        client.delete(f"/api/library/{_created.pop()}")


def _work(client, body: bytes = b"mirror-book-test") -> int:
    r = client.post("/api/library/upload",
                    files={"file": ("m.epub", body, "application/epub+zip")})
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    _created.append(wid)
    with Session(engine) as s:
        w = s.get(Work, wid)
        w.source_url = OLD
        w.chapters_count = 25
        s.add(w)
        s.commit()
    return wid


def _mons(wid: int) -> list[Monitored]:
    with Session(engine) as s:
        return s.exec(select(Monitored).where(Monitored.work_id == wid)).all()


def test_primary_mirror_retargets_book_and_subscribes(client):
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW, "primary": True})
    assert r.status_code == 200, r.text
    assert r.json()["source_url"] == NEW
    mons = _mons(wid)
    assert [m.source_url for m in mons] == [NEW]
    # Стартовое «видели» = главы книги: без этого первый тик перекачал бы всё.
    assert mons[0].last_seen_chapters == 25
    assert client.get(f"/api/library/{wid}").json()["monitored"] is True


def test_extra_mirror_keeps_primary_source(client):
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW})
    assert r.status_code == 200, r.text
    assert r.json()["source_url"] == OLD


def test_mirror_lifts_blacklist(client):
    from backend.app import blacklist
    wid = _work(client)
    with Session(engine) as s:
        blacklist.add_entry(s, urls=[NEW])
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW, "primary": True})
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert not blacklist.is_blacklisted(s, source_url=NEW)


def test_mirror_rejects_non_url_and_foreign_book(client):
    wid = _work(client)
    assert client.post(f"/api/library/{wid}/mirror", json={"url": "нет"}).status_code == 400
    other = _work(client, b"another-mirror-book")
    url = "https://ficbook.net/readfic/01a0ffff-0000-7000-8000-000000000001"
    assert client.post(f"/api/library/{other}/mirror", json={"url": url}).status_code == 200
    assert client.post(f"/api/library/{wid}/mirror", json={"url": url}).status_code == 409
