"""Скачать книгу по ссылке/названию и зарегистрировать в библиотеке.

Общая часть синхронного POST /api/ingest и фоновой очереди (ingestjob).
Старт и итог каждого скачивания пишутся в `reader.ingest` → journald → Loki
(serg/tasks#888).
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from sqlmodel import Session

from ..accounts import monitor, store
from ..downloaders import chain
from . import progress
from .db.models import IngestJob, Work
from .db.session import engine
from .ingestjob import mask, now
from .services import register_download

log = logging.getLogger("reader.ingest")


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def do_ingest(q: str, session: Session, job_id: str | None = None) -> Work:
    """Скачать, зарегистрировать и поставить на отслеживание.

    job_id — фоновое задание: его статус done уходит ТЕМ ЖЕ commit, что и
    регистрация книги. Иначе убийство процесса в окне «книга записана, задание
    ещё running» стоило бы повторного многоминутного скачивания после рестарта.
    """
    t0 = time.monotonic()
    shown = mask(q)
    log.info("скачивание начато: %s%s", shown, f" (задание {job_id})" if job_id else "")
    try:
        # Подставить креды аккаунта для домена (если есть) — для закрытого/18+.
        creds = store.creds_for_host(session, _host(q)) if chain.is_url(q) else None
        # Закрыть транзакцию ДО сети: иначе соединение с общим Postgres висит
        # «idle in transaction» всё скачивание (у «Червя» — около 7 минут).
        session.commit()
        result = chain.fetch(q, creds=creds)
        if job_id:
            job = session.get(IngestJob, job_id)
            if job is not None:
                t = now()
                job.status = "done"
                job.finished_at = t
                job.updated_at = t
                job.error = None
                job.title = result.title or job.title
                job.author = result.author or job.author
                job.source_host = _host(result.source_url or q)
                session.add(job)  # уйдёт commit'ом регистрации книги
        progress.report(stage="регистрация в библиотеке")
        work = register_download(result, session)
        if job_id:
            job = session.get(IngestJob, job_id)
            if job is not None:
                job.work_id = work.id
                job.title = work.title or job.title
                job.author = work.author or job.author
                job.chapters = work.chapters_count or 0
                session.add(job)
            session.commit()
        # Поставить фик на отслеживание обновлений. Метрику подписки задаёт адаптер,
        # если она НЕ равна числу глав: у документации Python это номер версии
        # (spec.reader.python-docs). Иначе подписка завелась бы с числом секций
        # файла в «версионных» единицах и требовала лишней перекачки.
        if work.source_url:
            metric = (result.extra or {}).get("update_metric") or work.chapters_count
            monitor.add_monitor(session, work.source_url, work.id, metric)
    except Exception as e:
        log.warning(
            "скачивание не удалось за %.1f с: %s — %s: %s",
            time.monotonic() - t0, shown, type(e).__name__, mask(str(e)),
        )
        raise
    log.info(
        "скачано за %.1f с: %s → work=%s «%s», глав %s",
        time.monotonic() - t0, shown, work.id, work.title, work.chapters_count,
    )
    return work


def run_ingest_job(job_id: str, query: str) -> None:
    with progress.activate(job_id), Session(engine) as session:
        progress.report(stage="поиск источника")
        do_ingest(query, session, job_id=job_id)
