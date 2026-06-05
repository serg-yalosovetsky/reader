"""APScheduler: периодический авто-импорт прогресса из бэкапа ReadEra.

Лёгкий in-process планировщик (без Redis). Включается, если задан интервал
READERA_SYNC_INTERVAL_MIN > 0 и настроена папка бэкапов.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from .config import (
    MONITOR_INTERVAL_MIN,
    READERA_BACKUP_REMOTE,
    READERA_SYNC_INTERVAL_MIN,
)
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


def _monitor_job() -> None:
    from ..accounts import monitor
    try:
        with Session(engine) as session:
            res = monitor.check_all(session, auto_download=True)
        log.info("Monitor check: %s", {k: res[k] for k in ("checked", "with_updates", "downloaded")})
    except Exception as e:  # noqa: BLE001
        log.warning("Monitor check failed: %s", e)


def start() -> None:
    global _scheduler
    if _scheduler:
        return
    jobs = []
    if READERA_SYNC_INTERVAL_MIN > 0 and READERA_BACKUP_REMOTE:
        jobs.append((_readera_import_job, READERA_SYNC_INTERVAL_MIN, "readera_import"))
    if MONITOR_INTERVAL_MIN > 0:
        jobs.append((_monitor_job, MONITOR_INTERVAL_MIN, "monitor_check"))
    if not jobs:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    for fn, minutes, jid in jobs:
        _scheduler.add_job(fn, "interval", minutes=minutes, id=jid)
    _scheduler.start()
    log.info("Scheduler started: %s", [j[2] for j in jobs])


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
