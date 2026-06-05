"""Роутер библиотеки: список произведений, карточка, загрузка файла вручную."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

import os

from .. import covers
from ..db.models import Monitored, Progress, Work, utcnow
from ..db.session import get_session
from ..services import _norm
from ..storage import detect_format, import_file, sha1_of_file

router = APIRouter(prefix="/api/library", tags=["library"])


def _fsize(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


@router.post("/maintenance")
def maintenance(session: Session = Depends(get_session)) -> dict:
    """Убрать дубликаты книг (оставить самый полный файл), подчистить мониторинг,
    добэкафиллить обложки."""
    # 1) Группировка по (название, автор).
    groups: dict[tuple, list[Work]] = {}
    for w in session.exec(select(Work)).all():
        groups.setdefault((_norm(w.title), _norm(w.author)), []).append(w)

    removed_works = 0
    for ws in groups.values():
        if len(ws) <= 1:
            continue
        ws.sort(key=lambda w: _fsize(w.file_path), reverse=True)  # самый полный — первым
        keep = ws[0]
        for dup in ws[1:]:
            for m in session.exec(select(Monitored).where(Monitored.work_id == dup.id)).all():
                m.work_id = keep.id
                session.add(m)
            for p in session.exec(select(Progress).where(Progress.work_id == dup.id)).all():
                session.delete(p)
            if dup.file_path and dup.file_path != keep.file_path:
                try:
                    os.remove(dup.file_path)
                except OSError:
                    pass
            session.delete(dup)
            removed_works += 1
    session.commit()

    # 2) Дедуп мониторинга по work_id / source_url.
    seen: set = set()
    removed_mon = 0
    for m in session.exec(select(Monitored)).all():
        key = ("w", m.work_id) if m.work_id else ("u", m.source_url)
        if key in seen:
            session.delete(m)
            removed_mon += 1
        else:
            seen.add(key)
    session.commit()

    # 3) Бэкафилл обложек.
    added_covers = 0
    for w in session.exec(select(Work)).all():
        if w.cover_path and os.path.exists(w.cover_path):
            continue
        if w.file_path and os.path.exists(w.file_path):
            c = covers.extract_cover(w.file_path, w.file_format, w.sha1)
            if c:
                w.cover_path = str(c)
                session.add(w)
                added_covers += 1
    session.commit()
    return {"removed_duplicates": removed_works, "removed_monitored": removed_mon,
            "covers_added": added_covers}


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
