"""Прогресс чтения: контракт с текстовым якорем (text_anchor), координаты одной
тройкой, ревизия файла, порядок досылки и «дочитано» (serg/tasks#1853)."""

from __future__ import annotations


def _upload(client, seed: str) -> dict:
    # Уникальные байты на тест → уникальный sha1 → отдельный Work (иначе upload
    # дедупит по sha1, и тесты делят одну книгу + прогресс через общую tmp-БД).
    body = f"epub-bytes-{seed}".encode()
    files = {"file": ("t.epub", body, "application/epub+zip")}
    r = client.post("/api/library/upload", files=files)
    assert r.status_code == 200
    return r.json()


def test_progress_roundtrip_text_anchor(client):
    wid = _upload(client, "roundtrip")["id"]
    anchor = "the quick brown fox jumps over"
    r = client.put(
        f"/api/progress/{wid}",
        json={"ratio": 0.5, "locator": "epubcfi(/6/4!/4)", "text_anchor": anchor},
    )
    assert r.status_code == 200
    assert r.json()["text_anchor"] == anchor

    g = client.get(f"/api/progress/{wid}")
    assert g.status_code == 200
    assert g.json()["text_anchor"] == anchor
    assert g.json()["locator"] == "epubcfi(/6/4!/4)"


def test_coordinates_are_written_as_one_triple(client):
    """Пустой якорь больше не оставляет прежний: строка склеивалась из двух
    страниц (CFI новой, якорь старой), и восстановление по якорю возвращало на
    старое место. Пустой якорь значит «восстанавливай по CFI»."""
    wid = _upload(client, "clobber")["id"]
    client.put(f"/api/progress/{wid}",
               json={"ratio": 0.3, "locator": "epubcfi(/6/4!/4)", "text_anchor": "старая страница"})
    client.put(f"/api/progress/{wid}",
               json={"ratio": 0.31, "locator": "epubcfi(/6/6!/2)", "text_anchor": "", "book_rev": "abc"})

    g = client.get(f"/api/progress/{wid}").json()
    assert (g["locator"], g["text_anchor"], g["book_rev"]) == ("epubcfi(/6/6!/2)", "", "abc")
    assert g["ratio"] == 0.31


def test_stale_replay_does_not_overwrite_newer(client):
    """Офлайн-хвост, досланный позже, не затирает место новее себя."""
    wid = _upload(client, "replay")["id"]
    client.put(f"/api/progress/{wid}",
               json={"ratio": 0.8, "locator": "new", "saved_at": 2_000_000_000_000})
    client.put(f"/api/progress/{wid}",
               json={"ratio": 0.2, "locator": "old", "saved_at": 1_900_000_000_000, "replay": True})
    assert client.get(f"/api/progress/{wid}").json()["locator"] == "new"
    # Более новая досылка — принимается.
    client.put(f"/api/progress/{wid}",
               json={"ratio": 0.9, "locator": "newer", "saved_at": 2_100_000_000_000, "replay": True})
    assert client.get(f"/api/progress/{wid}").json()["locator"] == "newer"


def _finished(client) -> list[int]:
    r = client.get("/api/progress/finished")
    assert r.status_code == 200, r.text
    return r.json()


def test_finished_only_on_last_page(client):
    """«Прочитано» — факт последней страницы, а не порог доли: 0.97 в последней
    главе (живой случай «Свет полярной звезды», гл. 27 из 29) — не прочитано."""
    wid = _upload(client, "finish")["id"]
    client.put(f"/api/progress/{wid}", json={"ratio": 0.9999, "locator": "x"})
    assert wid not in _finished(client)
    client.put(f"/api/progress/{wid}", json={"ratio": 0.9999, "locator": "x", "at_end": True})
    assert wid in _finished(client)
    # Перелистнуть назад — не «не дочитал».
    client.put(f"/api/progress/{wid}", json={"ratio": 0.5, "locator": "y"})
    assert wid in _finished(client)


def test_new_chapters_unfinish_book(client):
    from sqlmodel import Session
    from backend.app.db.models import Work
    from backend.app.db.session import engine

    wid = _upload(client, "finish-grow")["id"]
    with Session(engine) as s:
        w = s.get(Work, wid); w.chapters_count = 10; s.add(w); s.commit()
    client.put(f"/api/progress/{wid}", json={"ratio": 1.0, "locator": "x", "at_end": True})
    assert wid in _finished(client)
    with Session(engine) as s:
        w = s.get(Work, wid); w.chapters_count = 11; s.add(w); s.commit()
    assert wid not in _finished(client)
    client.put(f"/api/progress/{wid}", json={"ratio": 1.0, "locator": "z", "at_end": True})
    assert wid in _finished(client)


def test_backfill_marks_only_true_finish(client):
    from datetime import datetime
    from sqlmodel import Session, select
    from backend.app.db.models import Progress
    from backend.app.db.session import _backfill_finished, engine

    done = _upload(client, "bf-done")["id"]
    almost = _upload(client, "bf-almost")["id"]
    client.put(f"/api/progress/{done}", json={"ratio": 0.9999, "locator": "a"})
    client.put(f"/api/progress/{almost}", json={"ratio": 0.97, "locator": "b"})
    _backfill_finished()
    with Session(engine) as s:
        rows = {p.work_id: p for p in s.exec(select(Progress)).all()}
        assert isinstance(rows[done].finished_at, datetime)
        assert rows[almost].finished_at is None


def test_progress_default_has_empty_anchor(client):
    wid = _upload(client, "default")["id"]
    g = client.get(f"/api/progress/{wid}")
    assert g.status_code == 200
    assert g.json()["text_anchor"] == ""


def test_manual_finished_toggle(client):
    wid = _upload(client, "manual-finish")["id"]
    r = client.put(f"/api/progress/{wid}/finished", json={"finished": True})
    assert r.status_code == 200, r.text
    assert wid in _finished(client)
    client.put(f"/api/progress/{wid}/finished", json={"finished": False})
    assert wid not in _finished(client)
    assert client.put("/api/progress/999999/finished", json={"finished": True}).status_code == 404


def test_progress_format_roundtrip(client):
    """Формат и координаты заменяются вместе при переключении PDF/EPUB."""
    wid = _upload(client, "format-roundtrip")["id"]
    for fmt, locator in [("epub", "epubcfi(/6/4!/4)"), ("pdf", "epubcfi(/6/40)")]:
        r = client.put(f"/api/progress/{wid}", json={
            "ratio": 0.5, "locator": locator, "file_format": fmt,
        })
        assert r.status_code == 200
        g = client.get(f"/api/progress/{wid}").json()
        assert (g["ratio"], g["locator"], g["file_format"]) == (0.5, locator, fmt)


def test_progress_format_migration(monkeypatch):
    from sqlalchemy import text
    from sqlmodel import create_engine
    from backend.app.db import session as db

    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE progress (id INTEGER PRIMARY KEY, work_id INTEGER)'))
        conn.execute(text('INSERT INTO progress (id, work_id) VALUES (1, 1)'))
    monkeypatch.setattr(db, 'engine', engine)
    assert ('progress', 'file_format') in db._migrate_add_columns()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT file_format FROM progress')).scalar_one() == ''
    assert not db._migrate_add_columns()


def test_revision_conflict_and_replay(client):
    wid = _upload(client, "revision-conflict")["id"]
    rev = client.get(f"/api/library/{wid}").json()["file_rev"]
    assert len(rev) == 40
    file = client.get(f"/api/reader/{wid}/file")
    assert file.headers["x-book-rev"] == rev
    assert file.headers["etag"]
    assert client.get(f"/api/reader/{wid}/file", headers={"If-None-Match": file.headers["etag"]}).headers["x-book-rev"] == rev
    url = f"/api/progress/{wid}"
    client.put(url, json={"ratio": .8, "locator": "phone", "book_rev": rev, "saved_at": 200})
    rejected = client.put(url, json={"ratio": .2, "locator": "laptop", "book_rev": "old", "saved_at": 300})
    assert rejected.status_code == 409
    assert rejected.json()["file_rev"] == rev
    assert rejected.json()["progress"]["locator"] == "phone"
    assert client.get(url).json()["locator"] == "phone"
    assert client.get(url + "/history").json() == []
    replay = client.put(url, json={"ratio": .2, "book_rev": "old", "saved_at": 100, "replay": True})
    assert replay.status_code == 200
    assert replay.json()["locator"] == "phone"
    assert client.put(url, json={"ratio": .9, "locator": "replay", "book_rev": "old", "saved_at": 400, "replay": True}).json()["locator"] == "replay"


def test_overwrite_chapter_and_device(client):
    wid = _upload(client, "overwrite-chapter")["id"]
    url = f"/api/progress/{wid}"
    client.put(url, json={"ratio": .5, "locator": "old", "chapter": "chapter 1", "book_rev": "revision"})
    client.put(url, headers={"User-Agent": "Mozilla Android"}, json={"ratio": .501, "locator": "next", "chapter": "chapter 2"})
    row = client.get(url + "/history").json()[0]
    assert (row["locator"], row["chapter"], row["reason"], row["device"], row["book_rev"]) == ("old", "chapter 1", "overwrite", "android", "revision")
    client.put(url, headers={"User-Agent": "Windows NT"}, json={"ratio": .8, "locator": "jump", "chapter": "chapter 2"})
    assert client.get(url + "/history").json()[0]["device"] == "windows"


def test_history_migration(monkeypatch):
    from sqlalchemy import text
    from sqlmodel import create_engine
    from backend.app.db import session as db

    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE positionhistory (id INTEGER PRIMARY KEY, work_id INTEGER)'))
        conn.execute(text('INSERT INTO positionhistory (id, work_id) VALUES (1, 1)'))
    monkeypatch.setattr(db, 'engine', engine)
    added = db._migrate_add_columns()
    assert {('positionhistory', 'device'), ('positionhistory', 'book_rev')} <= added
    with engine.connect() as conn:
        assert conn.execute(text('SELECT device, book_rev FROM positionhistory')).one() == ('', '')
    assert not db._migrate_add_columns()


def test_revision_guard_direct(session, tmp_path, monkeypatch):
    """Exercise the transaction even when TestClient cannot start in a sandbox."""
    import hashlib
    import json
    from starlette.requests import Request
    from sqlmodel import select
    from backend.app.db.models import Work, PositionHistory
    from backend.app.routers import progress, library, reader
    from backend.app.db import session as db

    path = tmp_path / 'legacy.epub'
    path.write_bytes(b'current book')
    rev = hashlib.sha1(path.read_bytes()).hexdigest()
    work = Work(title='Test', file_path=str(path), file_format='epub')
    session.add(work)
    session.commit()
    monkeypatch.setattr(db, 'engine', session.get_bind())
    request = Request({'type': 'http', 'headers': [(b'user-agent', b'Android')]})
    def put(**data):
        return progress.set_progress(work.id, progress.ProgressIn(**data), request, session)
    assert library.get_work(work.id, session)['file_rev'] == rev
    response = reader.get_book_file(request, work.id)
    assert response.headers['x-book-rev'] == rev
    put(ratio=.7, locator='phone', chapter='20', book_rev=rev, saved_at=200)
    conflict = put(ratio=.4, locator='stale', book_rev='old', saved_at=300)
    assert conflict.status_code == 409
    assert json.loads(conflict.body)['progress']['locator'] == 'phone'
    assert put(ratio=.4, book_rev='old', saved_at=100, replay=True).locator == 'phone'
    assert session.exec(select(PositionHistory)).all() == []
    put(ratio=.701, locator='next', chapter='21', book_rev=rev)
    row = session.exec(select(PositionHistory)).one()
    assert (row.locator, row.book_rev, row.device, row.reason) == ('phone', rev, 'android', 'overwrite')


def test_revision_hash_cached_by_mtime(tmp_path, monkeypatch):
    import hashlib
    import os
    from backend.app.routers.reader import _path_sha1
    from backend.app import storage

    path = tmp_path / 'legacy.epub'
    path.write_bytes(b'first')
    calls = []
    def digest(path):
        calls.append(path)
        return hashlib.sha1(path.read_bytes()).hexdigest()
    monkeypatch.setattr(storage, 'sha1_of_file', digest)
    assert _path_sha1(path) == _path_sha1(path)
    assert len(calls) == 1
    stamp = path.stat().st_mtime_ns
    path.write_bytes(b'other')
    os.utime(path, ns=(stamp + 1_000_000, stamp + 1_000_000))
    assert _path_sha1(path) == hashlib.sha1(b'other').hexdigest()
    assert len(calls) == 2
    hashed = tmp_path / ('a' * 40 + '.epub')
    hashed.write_bytes(b'content addressed')
    assert _path_sha1(hashed) == 'a' * 40
    assert len(calls) == 2
