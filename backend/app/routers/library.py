"""Роутер библиотеки: список произведений, карточка, загрузка файла вручную."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

from ..db.models import Work, utcnow
from ..db.session import get_session
from ..storage import detect_format, import_file, sha1_of_file

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("")
def list_works(session: Session = Depends(get_session)) -> list[Work]:
    """Все произведения, новые сверху."""
    return list(session.exec(select(Work).order_by(Work.updated_at.desc())).all())


@router.get("/{work_id}")
def get_work(work_id: int, session: Session = Depends(get_session)) -> Work:
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    return work


@router.post("/upload")
async def upload_book(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> Work:
    """Ручная загрузка EPUB/FB2 (полезно на этапе 1 и как фоллбэк)."""
    fmt = detect_format(file.filename or "")
    if not fmt:
        raise HTTPException(400, "поддерживаются только .epub и .fb2")

    # Сохраняем во временный файл, считаем SHA-1, импортируем в хранилище.
    suffix = Path(file.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        sha1 = sha1_of_file(tmp_path)
        # Дедуп: если книга с таким SHA-1 уже есть — вернуть её.
        existing = session.exec(select(Work).where(Work.sha1 == sha1)).first()
        if existing:
            return existing
        dest, _ = import_file(tmp_path, sha1)
    finally:
        tmp_path.unlink(missing_ok=True)

    work = Work(
        title=Path(file.filename or "Без названия").stem,
        site="upload",
        file_path=str(dest),
        file_format=fmt,
        sha1=sha1,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(work)
    session.commit()
    session.refresh(work)
    return work
