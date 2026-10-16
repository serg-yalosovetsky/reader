"""Загрузка файлов в общей панели ingest, без повторного скачивания по имени файла."""
import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlmodel import Session, select

from . import ingestjob
from .db.models import IngestJob
from .db.session import engine

log = logging.getLogger("reader.ingest")


def create(filename: str) -> dict:
    t = ingestjob.now()
    with Session(engine) as session:
        # Незаконченный multipart мог вообще не попасть в обработчик (обрыв связи).
        for stale in session.exec(select(IngestJob).where(
            IngestJob.kind == "upload", IngestJob.status == "running",
            IngestJob.heartbeat_at < t - timedelta(hours=1),
        )).all():
            stale.status = "error"
            stale.error = "время ожидания файла истекло — отправьте файл заново"
            stale.finished_at = stale.updated_at = t
            session.add(stale)
        job_id = uuid.uuid4().hex
        row = IngestJob(
            id=job_id, kind="upload", query=f"upload:{job_id}:{filename[:400]}",
            title=filename[:500], status="running", max_attempts=1, attempts=1,
            created_at=t, updated_at=t, started_at=t, heartbeat_at=t,
            worker_unit=ingestjob.WORKER_UNIT, worker_boot=ingestjob.WORKER_BOOT,
            progress_stage="приём файла",
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return ingestjob._public(row)


def update(job_id: str, stage: str, *, works=None, error=None) -> None:
    with Session(engine) as session:
        row = session.get(IngestJob, job_id)
        if row is None or row.kind != "upload" or row.status != "running":
            raise HTTPException(409, "задание загрузки уже завершено или не найдено")
        row.progress_stage = stage
        row.updated_at = row.heartbeat_at = ingestjob.now()
        if works is not None:
            row.status = "done"
            row.finished_at = row.updated_at
            row.title = "; ".join(w["title"] for w in works)[:500]
            row.work_id = works[0]["id"] if works else None
        if error is not None:
            row.status = "error"
            row.error = ingestjob.mask(str(error))[:500]
            row.finished_at = row.updated_at
        session.add(row)
        session.commit()
    log.info("загрузка %s: %s%s", job_id, stage, f": {ingestjob.mask(str(error))}" if error else "")
