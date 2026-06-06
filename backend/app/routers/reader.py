"""Роутер чтения: отдаёт файл книги для рендера во foliate-js на клиенте."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from ..db.models import Work
from ..db.session import get_session

router = APIRouter(prefix="/api/reader", tags=["reader"])

_MEDIA = {"epub": "application/epub+zip", "fb2": "application/x-fictionbook+xml",
          "pdf": "application/pdf"}


@router.get("/{work_id}/file")
def get_book_file(work_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """Бинарь книги (EPUB/FB2). foliate-js грузит и рендерит его на клиенте."""
    work = session.get(Work, work_id)
    if not work or not work.file_path:
        raise HTTPException(404, "файл книги не найден")
    path = Path(work.file_path)
    if not path.exists():
        raise HTTPException(410, "файл книги отсутствует на диске")
    media = _MEDIA.get(work.file_format, "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


@router.get("/{work_id}/cover")
@router.head("/{work_id}/cover")
def get_cover(work_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """Обложка книги. Иначе 404 → фронт рисует заглушку."""
    work = session.get(Work, work_id)
    if not work or not work.cover_path:
        raise HTTPException(404, "обложки нет")
    path = Path(work.cover_path)
    if not path.exists():
        raise HTTPException(404, "файл обложки отсутствует")
    mtime = int(path.stat().st_mtime)
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    resp.headers["ETag"] = f'"{work.sha1}-{mtime}"'
    return resp
