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
