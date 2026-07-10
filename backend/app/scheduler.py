"""APScheduler: периодический авто-импорт прогресса из бэкапа ReadEra.

Лёгкий in-process планировщик (без Redis). Включается, если задан интервал
READERA_SYNC_INTERVAL_MIN > 0 и настроена папка бэкапов.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from .config import (
    CALIBRE_SYNC_INTERVAL_MIN,
    FICBOOK_FEED_INTERVAL_MIN,
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
    # Через тот же guard, что и кнопка «Обновления»: один _lock сериализует
    # плановый тик и ручной запуск → нет параллельных check_all и двойных докачек.
    from ..accounts import check_job
    try:
        res = check_job.run_blocking("scheduled")
        if res.get("status") == "skipped":
            log.info("Monitor check skipped: ручная проверка уже идёт")
        elif res.get("status") == "error":
            log.warning("Monitor check failed: %s", res.get("error"))
        else:
            r = res.get("result") or {}
            log.info("Monitor check: %s", {k: r.get(k) for k in ("checked", "with_updates", "downloaded")})
    except Exception as e:  # noqa: BLE001
        log.warning("Monitor check failed: %s", e)


def _calibre_sync_job() -> None:
    # Пере-синк каталога Calibre в Work-ссылки: новые книги появляются в
    # библиотеке ридера, метаданные обновляются. Файлы не копируются.
    from ..calibre import sync as csync
    try:
        with Session(engine) as session:
            res = csync.sync_catalog(session)
        log.info("Calibre catalog sync: %s", res)
    except Exception as e:  # noqa: BLE001
        log.warning("Calibre catalog sync failed: %s", e)


def _ficbook_feed_job() -> None:
    from ..accounts import feeds
    from ..accounts import check_job
    try:
        with Session(engine) as session:
            feeds.pull_all(session, sites=["ficbook"])
        # Сразу докачать книги с has_update=True (без полного счёта глав)
        res = check_job.run_blocking("ficbook_feed", only_pending=True, pull_feeds=False)
        if res.get("status") not in ("skipped", "error"):
            r = res.get("result") or {}
            if r.get("downloaded"):
                log.info("Ficbook auto-download: %s downloaded", r["downloaded"])
    except Exception as e:  # noqa: BLE001
        log.warning("Ficbook feed check failed: %s", e)


def start() -> None:
    global _scheduler
    if _scheduler:
        return
    jobs = []
    if READERA_SYNC_INTERVAL_MIN > 0 and READERA_BACKUP_REMOTE:
        jobs.append((_readera_import_job, READERA_SYNC_INTERVAL_MIN, "readera_import"))
    if FICBOOK_FEED_INTERVAL_MIN > 0:
        jobs.append((_ficbook_feed_job, FICBOOK_FEED_INTERVAL_MIN, "ficbook_feed"))
    if MONITOR_INTERVAL_MIN > 0:
        jobs.append((_monitor_job, MONITOR_INTERVAL_MIN, "monitor_check"))
    if CALIBRE_SYNC_INTERVAL_MIN > 0:
        jobs.append((_calibre_sync_job, CALIBRE_SYNC_INTERVAL_MIN, "calibre_sync"))
    if not jobs:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    for fn, minutes, jid in jobs:
        # coalesce: пропущенные тики схлопываются в один (не копим очередь).
        # misfire_grace_time: тик, задержавшийся из-за долгого предыдущего прогона,
        #   всё равно отрабатывает, а не тихо теряется.
        # max_instances=1: не плодим параллельные прогоны одного джоба.
        # ВАЖНО: это НЕ лечит зависший прогон — APScheduler не умеет прерывать
        #   уже запущенный поток. Единственная реальная защита от вечного клина
        #   счётчика инстансов — таймауты на всех сетевых вызовах внутри джоба
        #   (см. accounts/feeds.py _FICBOOK_TIMEOUT). Здесь — только гигиена.
        _scheduler.add_job(fn, "interval", minutes=minutes, id=jid,
                           max_instances=1, coalesce=True,
                           misfire_grace_time=300, replace_existing=True)
    _scheduler.start()
    log.info("Scheduler started: %s", [j[2] for j in jobs])


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
