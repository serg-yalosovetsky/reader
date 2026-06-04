"""Роутер Calibre: список книг библиотеки и импорт книги в читалку для чтения."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ...calibre import client as calibre
from ..db.models import Work, utcnow
from ..db.session import get_session
from ..storage import detect_format, import_file

router = APIRouter(prefix="/api/calibre", tags=["calibre"])


@router.get("/status")
def status() -> dict:
    return {"configured": calibre.is_configured()}


@router.get("/books")
def books() -> list[dict]:
    """Список книг из библиотеки Calibre (читается из metadata.db)."""
    return calibre.list_books()


@router.post("/import/{calibre_id}")
def import_book(calibre_id: int, session: Session = Depends(get_session)) -> Work:
    """Импортировать книгу из Calibre в читалку (копия в хранилище + Work),
    чтобы открыть её в веб-читалке и синхронизировать прогресс."""
    # Уже импортирована?
    existing = session.exec(select(Work).where(Work.calibre_id == calibre_id)).first()
    if existing:
        return existing

    src = calibre.book_file_path(calibre_id)
    if not src or not Path(src).exists():
        raise HTTPException(404, "файл книги в Calibre не найден (нужен EPUB/FB2)")

    fmt = detect_format(src.name)
    if not fmt:
        raise HTTPException(415, "поддерживаются EPUB/FB2")

    dest, sha1 = import_file(src)
    meta = next((b for b in calibre.list_books() if b["calibre_id"] == calibre_id), {})
    work = Work(
        title=meta.get("title", src.stem),
        author=meta.get("authors", ""),
        site="calibre",
        file_path=str(dest),
        file_format=fmt,
        sha1=sha1,
        calibre_id=calibre_id,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(work)
    session.commit()
    session.refresh(work)
    return work
