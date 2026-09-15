"""Прогресс фонового скачивания для панели «Скачивания» (serg/tasks#893).

Адаптеры зовут report(...) в своих циклах. Вне фонового задания (синхронный
POST /api/ingest, докачка монитором) вызов ничего не делает — привязка к
заданию живёт в threading.local, её ставит воркер очереди через activate().

Запись — короткий UPDATE строки ingest_job в собственной транзакции, не чаще
MIN_INTERVAL_S на задание (плюс всегда: смена стадии, название, последний шаг).
Иначе панель, опрашивающая прогресс, стала бы потоком апдейтов в общий Postgres.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from sqlalchemy import update
from sqlmodel import col

log = logging.getLogger("reader.ingest")

MIN_INTERVAL_S = 2.0
_local = threading.local()


@contextlib.contextmanager
def activate(job_id: str):
    prev = getattr(_local, "state", None)
    _local.state = {"job_id": job_id, "last": 0.0, "stage": None}
    try:
        yield
    finally:
        _local.state = prev


def active_job() -> str | None:
    st = getattr(_local, "state", None)
    return st["job_id"] if st else None


def report(
    done: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    stage: str | None = None,
    title: str | None = None,
) -> None:
    st = getattr(_local, "state", None)
    if st is None:
        return
    t = time.monotonic()
    last_step = done is not None and total is not None and done >= total
    stage_changed = stage is not None and stage != st["stage"]
    if not (last_step or stage_changed or title or t - st["last"] >= MIN_INTERVAL_S):
        return
    values: dict = {}
    if done is not None:
        values["progress_done"] = int(done)
    if total is not None:
        values["progress_total"] = int(total)
    if unit:
        values["progress_unit"] = unit
    if stage:
        values["progress_stage"] = stage
    if title:
        values["title"] = title[:500]
    if not values:
        return
    from .db.models import IngestJob
    from .db.session import engine
    from .ingestjob import now

    values["heartbeat_at"] = values["updated_at"] = now()
    try:
        with engine.begin() as conn:
            conn.execute(
                update(IngestJob)
                .where(col(IngestJob.id) == st["job_id"], col(IngestJob.status) == "running")
                .values(**values)
            )
    except Exception as e:  # noqa: BLE001 — прогресс не роняет скачивание, но и не молчит
        log.warning("прогресс задания %s не записан: %s", st["job_id"], e)
        return
    st["last"] = t
    if stage:
        st["stage"] = stage
