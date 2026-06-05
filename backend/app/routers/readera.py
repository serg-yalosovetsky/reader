"""Роутер синхронизации с ReadEra: статус, sync, импорт/экспорт, ручная загрузка .bak."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from sqlmodel import Session

from ...readera import gdrive, sync
from ..db.session import get_session

router = APIRouter(prefix="/api/readera", tags=["readera"])


@router.get("/status")
def status() -> dict:
    return {"available": gdrive.available(), "latest_backup": gdrive.latest_backup()}


@router.post("/sync")
def run_sync(session: Session = Depends(get_session)) -> dict:
    return sync.sync(session)


@router.post("/import")
def run_import(session: Session = Depends(get_session)) -> dict:
    return sync.import_progress(session)


@router.post("/export")
def run_export(session: Session = Depends(get_session)) -> dict:
    return sync.export_progress(session)


@router.post("/upload-backup")
async def upload_backup(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> dict:
    """Фоллбэк без rclone: загрузить .bak вручную и импортировать прогресс."""
    tmp = Path(tempfile.mkdtemp(prefix="readera_up_")) / (file.filename or "backup.bak")
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return sync.import_progress(session, bak_path=tmp)
