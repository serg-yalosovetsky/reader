"""Вытеснение кэша Calibre: что можно выбрасывать, а что нельзя.

Кэш конечен, и очередь на вылет решает, будет ли открытие книги мгновенным.
Замер на живом кэше показал перекос: 97 файлов из 104 не открывались ни разу и
занимали 1904 МБ из 1960 МБ, а книгам, которые читают, оставалось 88 МБ — их
вытесняло, и каждое открытие снова тянуло файл с роутера.

Отдельно: «есть готовая EPUB-версия» — не основание удалять PDF-оригинал. По
original=1 читалка отдаёт именно его, потому что у части книг вёрстка PDF
читается лучше перетекающего текста.
"""

from __future__ import annotations

import os

from sqlmodel import Session, SQLModel

from backend.app.db.models import Progress, Work

OLD = (1_600_000_000, 1_600_000_000)


def _add_book(engine, calibre_id: int, *, read: bool, converted: str = "") -> None:
    """Завести книгу в БД; read=True — её открывали (есть прогресс)."""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        work = Work(
            title=f"book-{calibre_id}",
            site="calibre",
            calibre_id=calibre_id,
            file_format="pdf",
            converted_status="ready" if converted else "",
            converted_path=converted,
        )
        s.add(work)
        s.commit()
        s.refresh(work)
        if read:
            s.add(Progress(work_id=work.id))
            s.commit()


def test_nothing_is_deleted_while_cache_has_room(tmp_path, monkeypatch):
    """Пока лимит не превышен, не удаляется ничего — включая PDF с готовым EPUB.

    Оригинал нужен для original=1: у части книг вёрстка PDF читается лучше.
    """
    from backend.app.db.session import engine
    from backend.calibre import sync

    cache = tmp_path / "cache"
    cache.mkdir()
    epub = tmp_path / "77.epub"
    epub.write_bytes(b"converted")
    pdf = cache / "77.pdf"
    pdf.write_bytes(b"x" * 1024)

    _add_book(engine, 77, read=True, converted=str(epub))
    monkeypatch.setattr(sync, "CALIBRE_CACHE_DIR", cache)
    monkeypatch.setattr(sync, "CALIBRE_CACHE_MAX_MB", 100)  # места вдоволь

    sync._evict_cache()

    assert pdf.exists(), "PDF-оригинал удалён при свободном кэше — original=1 сломан"


def test_unread_book_evicted_before_the_one_being_read(tmp_path, monkeypatch):
    """При нехватке места первой уходит книга, которую ни разу не открывали.

    Чистый LRU по mtime выбросил бы здесь ровно наоборот: читаемая книга
    старее, а непрочитанная свежее.
    """
    from backend.app.db.session import engine
    from backend.calibre import sync

    cache = tmp_path / "cache"
    cache.mkdir()

    unread = cache / "88.pdf"  # свежая, но её никогда не открывали
    unread.write_bytes(b"x" * 900 * 1024)
    reading = cache / "99.pdf"  # старая, но она в работе
    reading.write_bytes(b"y" * 400 * 1024)
    os.utime(reading, OLD)

    _add_book(engine, 88, read=False)
    _add_book(engine, 99, read=True)
    monkeypatch.setattr(sync, "CALIBRE_CACHE_DIR", cache)
    monkeypatch.setattr(sync, "CALIBRE_CACHE_MAX_MB", 1)  # 1 МБ при 1.3 МБ данных

    sync._evict_cache()

    assert not unread.exists(), "непрочитанная книга должна уходить первой"
    assert reading.exists(), "выбросили книгу, которую читают, — ради непрочитанной"


def test_orphan_file_goes_first(tmp_path, monkeypatch):
    """Файл, которому не соответствует книга в БД, — самый первый кандидат."""
    from backend.app.db.session import engine
    from backend.calibre import sync

    cache = tmp_path / "cache"
    cache.mkdir()

    orphan = cache / "4242.pdf"  # такой книги в БД нет
    orphan.write_bytes(b"x" * 900 * 1024)
    reading = cache / "55.pdf"
    reading.write_bytes(b"y" * 400 * 1024)
    os.utime(reading, OLD)  # сирота свежее — чистый LRU убрал бы не её

    _add_book(engine, 55, read=True)
    monkeypatch.setattr(sync, "CALIBRE_CACHE_DIR", cache)
    monkeypatch.setattr(sync, "CALIBRE_CACHE_MAX_MB", 1)

    sync._evict_cache()

    assert not orphan.exists(), "сирота должна уходить первой"
    assert reading.exists(), "книгу в работе выбросили раньше сироты"
