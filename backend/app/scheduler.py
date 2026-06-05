"""APScheduler: периодический авто-импорт прогресса из бэкапа ReadEra.

Лёгкий in-process планировщик (без Redis). Включается, если задан интервал
READERA_SYNC_INTERVAL_MIN > 0 и настроена папка бэкапов.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from .config import READERA_BACKUP_REMOTE, READERA_SYNC_INTERVAL_MIN
from .db.session import engine

log = logging.getLogger("reader.scheduler")
_scheduler: BackgroundScheduler | None = None


def _readera_import_job() -> None:
    from ..readera import sync
    try:
        with Session(engine) as session:
            res = sync.import_progress(session)
        log.info("ReadEra auto-import: %s", res)
    except Exception as e:  # noqa: BLE001 — фон, не роняем планировщик
        log.warning("ReadEra auto-import failed: %s", e)


def start() -> None:
    global _scheduler
    if _scheduler:
        return
    if READERA_SYNC_INTERVAL_MIN <= 0 or not READERA_BACKUP_REMOTE:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _readera_import_job, "interval",
        minutes=READERA_SYNC_INTERVAL_MIN, id="readera_import",
    )
    _scheduler.start()
    log.info("Scheduler started: ReadEra import every %d min", READERA_SYNC_INTERVAL_MIN)


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
