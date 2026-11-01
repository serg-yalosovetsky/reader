"""Тесты удаления книги из библиотеки (DELETE /api/library/{id}) — serg/tasks#2565.

Проверяет:
- удаление книги с историей позиций (PositionHistory) без ForeignKeyViolation (200 OK);
- удаление строк из всех дочерних таблиц с FK на work.id;
- удаление книги из библиотеки (отсутствует в списке и 404 на детальной странице);
- корректную очистку файлов на диске и устойчивость к уже отсутствующим файлам.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from backend.app.db.models import (
    Bookmark,
    ChapterMeta,
    Highlight,
    Monitored,
    PositionHistory,
    Progress,
    Work,
)
from backend.app.db.session import engine


def _upload_book(client, name: str = "book-delete-test") -> int:
    files = {"file": (f"{name}.epub", f"content-{name}".encode(), "application/epub+zip")}
    r = client.post("/api/library/upload", files=files)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_delete_work_with_position_history(client):
    """Книга с positionhistory удаляется, DELETE -> 200, строки positionhistory

    этой книги удалены, книга не в библиотеке (serg/tasks#2565).
    """
    wid = _upload_book(client, "pos-history")

    # Создаём несколько записей в истории позиций
    h1 = client.post(
        f"/api/progress/{wid}/history",
        json={"ratio": 0.1, "locator": "loc-1", "text_anchor": "anchor-1", "reason": "open"},
    )
    assert h1.status_code == 200, h1.text

    h2 = client.post(
        f"/api/progress/{wid}/history",
        json={"ratio": 0.25, "locator": "loc-2", "text_anchor": "anchor-2", "reason": "jump"},
    )
    assert h2.status_code == 200, h2.text

    # Проверяем, что записи истории действительно записались в БД
    with Session(engine) as session:
        hist_rows = session.exec(
            select(PositionHistory).where(PositionHistory.work_id == wid)
        ).all()
        assert len(hist_rows) == 2

    # Удаляем книгу через API
    del_res = client.delete(f"/api/library/{wid}")
    assert del_res.status_code == 200, del_res.text
    assert del_res.json() == {"ok": True}

    # Проверяем, что книга не в библиотеке
    lib_res = client.get("/api/library")
    assert lib_res.status_code == 200
    lib_ids = [w["id"] for w in lib_res.json()]
    assert wid not in lib_ids

    # Детальная страница книги отдаёт 404
    assert client.get(f"/api/library/{wid}").status_code == 404

    # В базе: Work удалена, строки PositionHistory для этой книги удалены
    with Session(engine) as session:
        assert session.get(Work, wid) is None
        hist_after = session.exec(
            select(PositionHistory).where(PositionHistory.work_id == wid)
        ).all()
        assert len(hist_after) == 0


def test_delete_work_cleans_all_child_tables(client):
    """Удаление книги удаляет строки во ВСЕХ дочерних таблицах с FK на work.id:

    Progress, PositionHistory, ChapterMeta, Bookmark, Highlight, Monitored.
    """
    wid = _upload_book(client, "all-children")

    # Заполняем все дочерние таблицы данными
    with Session(engine) as session:
        session.add(Progress(work_id=wid, ratio=0.5, locator="loc-p"))
        session.add(PositionHistory(work_id=wid, ratio=0.3, locator="loc-ph", reason="jump"))
        session.add(ChapterMeta(work_id=wid, number=1, title="Глава 1", url="http://ch/1"))
        session.add(Bookmark(work_id=wid, ratio=0.4, locator="loc-bm", label="закладка"))
        session.add(Highlight(work_id=wid, ratio=0.45, locator="loc-hl", text="цитата"))
        session.add(Monitored(work_id=wid, source_url="http://site/book/1", last_seen_chapters=5))
        session.commit()

    # Проверяем наличие строк перед удалением
    with Session(engine) as session:
        for model in (Progress, PositionHistory, ChapterMeta, Bookmark, Highlight, Monitored):
            rows = session.exec(select(model).where(model.work_id == wid)).all()
            assert len(rows) >= 1, f"Ожидалась запись в {model.__name__}"

    # Удаляем книгу
    del_res = client.delete(f"/api/library/{wid}")
    assert del_res.status_code == 200, del_res.text

    # Проверяем, что во всех дочерних таблицах строки этой книги удалены
    with Session(engine) as session:
        assert session.get(Work, wid) is None
        for model in (Progress, PositionHistory, ChapterMeta, Bookmark, Highlight, Monitored):
            rows = session.exec(select(model).where(model.work_id == wid)).all()
            assert len(rows) == 0, f"Остались записи в {model.__name__} после удаления"


def test_delete_work_files_and_missing_files(client):
    """Удаление книги удаляет файл с диска и устойчиво к уже отсутствующим файлам."""
    wid = _upload_book(client, "files-clean")

    with Session(engine) as session:
        w = session.get(Work, wid)
        file_path = Path(w.file_path)
        assert file_path.exists()

    # Удаляем книгу
    r = client.delete(f"/api/library/{wid}")
    assert r.status_code == 200
    assert not file_path.exists()

    # Повторное удаление уже удалённой книги -> 404
    assert client.delete(f"/api/library/{wid}").status_code == 404


def test_dedup_works_rebinds_bookmarks_and_highlights_to_keep(client, tmp_path):
    """При схлопывании дублей (_dedup_works) закладки и выделения дубля

    не удаляются молча, а перепривязываются на keep.id.
    """
    from backend.app.routers.library import _dedup_works

    f_keep = tmp_path / "keep.epub"
    f_keep.write_bytes(b"longer book file content" * 20)
    f_dup = tmp_path / "dup.epub"
    f_dup.write_bytes(b"short")

    with Session(engine) as session:
        keep = Work(
            title="Война и мир",
            author="Лев Толстой",
            site="upload",
            file_path=str(f_keep),
            file_format="epub",
            sha1="sha1-keep-dedup",
        )
        dup = Work(
            title="Война и мир",
            author="Лев Толстой",
            site="upload",
            file_path=str(f_dup),
            file_format="epub",
            sha1="sha1-dup-dedup",
        )
        session.add(keep)
        session.add(dup)
        session.commit()
        session.refresh(keep)
        session.refresh(dup)
        keep_id, dup_id = keep.id, dup.id

        # У keep есть своя закладка
        session.add(Bookmark(work_id=keep_id, locator="loc-keep-1", label="Keep Mark", ratio=0.1))
        # У дубля есть закладка и выделение
        session.add(Bookmark(work_id=dup_id, locator="loc-dup-1", label="Dup Mark", ratio=0.3))
        session.add(Highlight(work_id=dup_id, locator="loc-dup-hl", text="Dup Quote", color="yellow", ratio=0.35))
        # И одна дублирующая закладка, идентичная keep
        session.add(Bookmark(work_id=dup_id, locator="loc-keep-1", label="Keep Mark", ratio=0.1))
        session.commit()

        # Запускаем схлопывание дублей
        removed = _dedup_works(session)
        assert removed == 1

        # Дубль удалён, выживший остался
        assert session.get(Work, dup_id) is None
        assert session.get(Work, keep_id) is not None

        # Закладки: собственная keep + перенесённая от dup (дублирующая удалена)
        keep_bms = session.exec(select(Bookmark).where(Bookmark.work_id == keep_id)).all()
        assert len(keep_bms) == 2
        labels = {b.label for b in keep_bms}
        assert labels == {"Keep Mark", "Dup Mark"}

        # Выделения: перенесены на keep
        keep_hls = session.exec(select(Highlight).where(Highlight.work_id == keep_id)).all()
        assert len(keep_hls) == 1
        assert keep_hls[0].text == "Dup Quote"
        assert keep_hls[0].work_id == keep_id

        # У дубля не осталось дочерних записей
        assert len(session.exec(select(Bookmark).where(Bookmark.work_id == dup_id)).all()) == 0
        assert len(session.exec(select(Highlight).where(Highlight.work_id == dup_id)).all()) == 0
