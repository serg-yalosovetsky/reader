"""Дочитанная книга, в которую докачали главы, перестаёт считаться дочитанной.

Живой случай: «Парадокс второго шанса» — 197 глав, ratio 0.9929, карточка
показывала зелёную галочку «прочитано» и прятала плашку обновления, хотя новые
главы человек не открывал. Карточка на это и рассчитывала («докачается глава —
ratio упадёт ниже порога»), но хранимая доля считалась от прежнего объёма и не
падала никогда.
"""

from __future__ import annotations

from sqlmodel import Session, select

from backend.app.db.models import Work
from backend.app.db.session import engine


def _upload(client, seed: str) -> dict:
    body = f"epub-bytes-{seed}".encode()
    files = {"file": ("t.epub", body, "application/epub+zip")}
    r = client.post("/api/library/upload", files=files)
    assert r.status_code == 200
    return r.json()


def _set_chapters(work_id: int, count: int) -> None:
    """Сымитировать докачку: у книги стало больше глав."""
    with Session(engine) as s:
        work = s.exec(select(Work).where(Work.id == work_id)).one()
        work.chapters_count = count
        s.add(work)
        s.commit()


READ_THRESHOLD = 0.98  # порог «прочитано» на карточке (frontend/js/library.js)


def test_progress_records_chapter_count_at_read(client):
    wid = _upload(client, "records-count")["id"]
    _set_chapters(wid, 100)

    client.put(
        f"/api/progress/{wid}", json={"ratio": 0.5, "locator": "", "text_anchor": "x"}
    )

    got = client.get(f"/api/progress/{wid}").json()
    assert got["chapters_at_read"] == 100, "объём на момент чтения должен сохраняться"


def test_finished_book_stops_being_read_after_new_chapters(client):
    wid = _upload(client, "grown")["id"]
    _set_chapters(wid, 190)
    # Дочитал до конца прежнего объёма.
    client.put(
        f"/api/progress/{wid}",
        json={"ratio": 0.99, "locator": "", "text_anchor": "конец"},
    )
    assert client.get(f"/api/progress/{wid}").json()["ratio"] >= READ_THRESHOLD

    # FanFicFare докачал главы: 190 → 240.
    _set_chapters(wid, 240)

    single = client.get(f"/api/progress/{wid}").json()
    assert single["ratio"] < READ_THRESHOLD, "книга не должна оставаться «прочитанной»"
    assert abs(single["ratio"] - 0.99 * 190 / 240) < 1e-6

    listed = client.get("/api/progress").json()
    assert listed[str(wid)] < READ_THRESHOLD, "в списке для карточек — та же поправка"


def test_reading_the_new_chapters_makes_it_read_again(client):
    wid = _upload(client, "caught-up")["id"]
    _set_chapters(wid, 190)
    client.put(
        f"/api/progress/{wid}",
        json={"ratio": 0.99, "locator": "", "text_anchor": "конец"},
    )
    _set_chapters(wid, 240)
    assert client.get(f"/api/progress/{wid}").json()["ratio"] < READ_THRESHOLD

    # Человек дочитал новые главы — доля снова от актуального объёма.
    client.put(
        f"/api/progress/{wid}",
        json={"ratio": 0.99, "locator": "", "text_anchor": "новый конец"},
    )
    again = client.get(f"/api/progress/{wid}").json()
    assert again["ratio"] >= READ_THRESHOLD, (
        "дочитанная в новом объёме — снова прочитана"
    )
    assert again["chapters_at_read"] == 240


def test_shrinking_or_equal_book_keeps_ratio(client):
    """Книга не выросла — доля не трогается (в том числе если глав стало меньше)."""
    wid = _upload(client, "same-size")["id"]
    _set_chapters(wid, 100)
    client.put(
        f"/api/progress/{wid}", json={"ratio": 0.42, "locator": "", "text_anchor": "s"}
    )

    assert abs(client.get(f"/api/progress/{wid}").json()["ratio"] - 0.42) < 1e-9
    _set_chapters(wid, 80)
    assert abs(client.get(f"/api/progress/{wid}").json()["ratio"] - 0.42) < 1e-9


def test_old_rows_without_chapter_count_are_untouched(client):
    """Записи, сделанные до появления поля, не должны «худеть» на ровном месте."""
    wid = _upload(client, "legacy")["id"]
    _set_chapters(wid, 100)
    client.put(
        f"/api/progress/{wid}", json={"ratio": 0.99, "locator": "", "text_anchor": "s"}
    )

    from backend.app.db.models import Progress

    with Session(engine) as s:
        prog = s.exec(select(Progress).where(Progress.work_id == wid)).one()
        prog.chapters_at_read = 0  # как у строк, записанных старой версией
        s.add(prog)
        s.commit()
    _set_chapters(wid, 500)

    assert abs(client.get(f"/api/progress/{wid}").json()["ratio"] - 0.99) < 1e-9


def test_content_updated_at_is_not_touched_by_reading(client):
    """«Обновлено» — про новые главы. Чтение не должно его двигать."""
    wid = _upload(client, "dates")["id"]
    before = client.get(f"/api/library/{wid}").json()
    assert before["content_updated_at"], "загрузка книги — это изменение содержимого"

    client.put(
        f"/api/progress/{wid}", json={"ratio": 0.3, "locator": "", "text_anchor": "s"}
    )

    after = client.get(f"/api/library/{wid}").json()
    assert after["content_updated_at"] == before["content_updated_at"], (
        "дата выхода глав не должна меняться от того, что книгу читают"
    )
    assert after["updated_at"] != before["updated_at"], (
        "а вот «последняя активность» — должна"
    )
