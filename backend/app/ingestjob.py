"""Фоновые задания добавления книги (POST /api/ingest с background=true).

Зачем фон: большая книга качается минутами — «Червь» (ficbook, 311 глав) шёл
~7 минут, — а nginx рвёт синхронный запрос по proxy_read_timeout (300 с).
Пользователь видел «504 Gateway Time-out», хотя поток докачивал книгу и
регистрировал её в библиотеке (serg/tasks#887). Здесь HTTP отвечает сразу, итог
забирается опросом GET /api/ingest/jobs/{job_id}.

In-memory состояние корректно ТОЛЬКО при uvicorn --workers 1 (так и запущен
reader.service). После рестарта задания теряются: опрос получит 404, и фронт
скажет это прямо, а не будет ждать вечно. Одновременно качаются не больше
_SLOTS книг: FanFicFare — отдельный процесс на ~200 МБ, у юнита MemoryMax 1G;
остальные честно стоят в статусе «queued».
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

from ..downloaders.base import DownloaderError

log = logging.getLogger("reader.ingest")

_SLOTS = threading.BoundedSemaphore(2)
_KEEP = 50  # сколько последних заданий помнить для опроса
_lock = threading.Lock()
_jobs: "OrderedDict[str, dict]" = OrderedDict()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(job: dict) -> dict:
    snap = {k: v for k, v in job.items() if not k.startswith("_")}
    t0, t1 = job.get("_t0"), job.get("_t1")
    snap["elapsed_s"] = round((t1 or time.monotonic()) - t0, 1) if t0 else None
    return snap


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def start(query: str, runner: Callable[[], dict]) -> dict:
    """Поставить скачивание в фон. runner возвращает краткое описание книги."""
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "queued",  # queued | running | done | error
        "query": query,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "work": None,
        "error": None,
        "_t0": None,
        "_t1": None,
    }
    with _lock:
        _jobs[job_id] = job
        while len(_jobs) > _KEEP:
            _jobs.popitem(last=False)
        # Снимок ДО старта потока и под блокировкой: иначе быстрый runner успевает
        # перевести задание в done/error раньше, чем мы прочитаем его без замка, —
        # ответ POST становился недетерминированным (гонка поймана тестом).
        snap = _public(job)
    threading.Thread(
        target=_run, args=(job, runner), daemon=True, name=f"ingest-{job_id}"
    ).start()
    return snap


def _finish(job: dict, *, work: dict | None = None, error: str | None = None) -> None:
    with _lock:
        job.update(
            status="error" if error else "done",
            finished_at=_now(),
            work=work,
            error=error,
            _t1=time.monotonic(),
        )


def _run(job: dict, runner: Callable[[], dict]) -> None:
    with _SLOTS:
        with _lock:
            job.update(status="running", started_at=_now(), _t0=time.monotonic())
        try:
            work = runner()
        except DownloaderError as e:
            # Причину с длительностью runner уже записал warning'ом; здесь —
            # статус, который увидит опрос.
            _finish(job, error=str(e)[:500])
        except Exception as e:  # noqa: BLE001 — фон: поток не роняем, но и не молчим
            log.exception("фоновое добавление упало: %s", job["query"])
            _finish(job, error=f"{type(e).__name__}: {e}"[:500])
        else:
            _finish(job, work=work)
