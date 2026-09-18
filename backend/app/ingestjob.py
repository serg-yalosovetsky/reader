"""Очередь фоновых заданий скачивания книг — в БД (serg/tasks#892).

Зачем: большая книга качается минутами («Червь», ficbook, 311 глав — ~7 минут),
а задания жили в памяти процесса. `systemctl restart reader` убивал поток и
подпроцесс FanFicFare, задание пропадало без следа: опрос получал 404, в логе не
было даже строки об обрыве. Теперь очередь — таблица `ingest_job`.

Правила (разбор architect, 2026-09-15):
- Воркеры — фиксированный пул потоков в процессе (SLOTS). POST только пишет
  строку и будит пул; поток на запрос больше не создаётся.
- «Своё» задание определяется по worker_unit (стабилен между рестартами) и
  worker_boot (INVOCATION_ID systemd, новый на каждый запуск). Dev-копия ридера на
  той же БД — другой юнит, её задания и задания прода друг друга не трогают.
- Плановая остановка (lifespan shutdown) возвращает свои running в очередь БЕЗ
  штрафа попытки: деплой — не провал задания.
- При старте свои running от прошлого запуска — это крэш/OOM/kill: в очередь с
  отсрочкой CRASH_RETRY_DELAY, попытка засчитана. После max_attempts запусков
  задание не возобновляется — иначе задание, которое само роняет процесс,
  устроит карусель рестартов.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from ..downloaders.base import DownloaderError
from .db.models import IngestJob
from .db.session import engine

log = logging.getLogger("reader.ingest")

SLOTS = 2  # FanFicFare — отдельный процесс ~200 МБ, у юнита MemoryMax 1G
MAX_ACTIVE = 20  # queued + running; сверх — 429, а не бесконечная очередь
POLL_S = 3.0
CRASH_RETRY_DELAY = timedelta(seconds=10)
# Пауза перед повтором после ОБЫЧНОЙ ошибки скачивания (сеть моргнула, сайт
# придержал запрос). Больше крэш-отсрочки: там процесс уже мёртв и ждать нечего,
# а здесь имеет смысл дать источнику отдышаться (serg/tasks#986).
ERROR_RETRY_DELAY = timedelta(seconds=60)
RETENTION = timedelta(days=7)

_HOST = socket.gethostname()


def _detect_unit(cgroup_text: str, host: str, pid: int) -> str:
    """Имя systemd-юнита процесса из его cgroup: «reader.service@host».

    НЕ по INVOCATION_ID: systemd выставляет его ЛЮБОМУ юниту, и диагностический
    процесс из транзиентного юнита mesh-ops счёл себя ридером (проверка
    2026-09-15) — его recover_on_startup вернул бы в очередь живые задания прода.
    Вне service-юнита (ручной запуск, тесты) — «manual@host:pid»: такой процесс
    не считает своими ничьи прерванные задания, кроме собственных.
    """
    for line in (cgroup_text or "").splitlines():
        path = line.rsplit(":", 1)[-1]
        for seg in reversed(path.strip("/").split("/")):
            if seg.endswith(".service"):
                return f"{seg}@{host}"
    return f"manual@{host}:{pid}"


def _read_own_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        # Не Linux или /proc недоступен: юнит определится как manual@host:pid —
        # безопасный вариант (чужие задания не трогаем), но скажем об этом.
        log.warning("очередь скачиваний: не прочитать /proc/self/cgroup (%s) — воркер будет manual", e)
        return ""


WORKER_UNIT = _detect_unit(_read_own_cgroup(), _HOST, os.getpid())
WORKER_BOOT = os.environ.get("INVOCATION_ID") or uuid.uuid4().hex

_ACTIVE = ("queued", "running")
_enqueue_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()
_threads: list[threading.Thread] = []
_generation = 0


class QueueFull(Exception):
    """Активных заданий уже MAX_ACTIVE."""


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_USERINFO = re.compile(r"(\w+://)[^/@\s]+@")


def mask(text: str | None) -> str:
    """Скрыть логин/пароль в URL (https://user:pass@host → https://***@host)."""
    return _USERINFO.sub(r"\1***@", text or "")


def _log_failure(message: str, *args, exc: BaseException) -> None:
    """Сбой с трассировкой, но без секретов.

    log.exception дописывает traceback с сырым str(e) сам, мимо mask(): ссылка
    вида https://user:pass@host из текста исключения уходила бы в общий Loki
    немаскированной (приёмка serg/tasks#892, security-reviewer HIGH). Трассировку
    форматируем сами и маскируем целиком — по ней по-прежнему можно чинить.
    """
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error(message + "\n%s", *args, mask(tb))


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def _public(row: IngestJob, **extra) -> dict:
    elapsed = None
    if row.started_at:
        elapsed = round(((row.finished_at or now()) - row.started_at).total_seconds(), 1)
    work = None
    if row.status == "done" and row.work_id is not None:
        work = {"id": row.work_id, "title": row.title, "author": row.author, "chapters": row.chapters}
    snap = {
        "job_id": row.id,
        "kind": row.kind,
        "status": row.status,
        "query": mask(row.query),
        "title": row.title,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "elapsed_s": elapsed,
        "work": work,
        "error": row.error,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "interruptions": row.interruptions,
        "interrupted_by": row.interrupted_by,
        "progress": {
            "done": row.progress_done,
            "total": row.progress_total,
            "unit": row.progress_unit,
            "stage": row.progress_stage,
        },
    }
    snap.update(extra)
    return snap


def _active_by_query(s: Session, query: str) -> IngestJob | None:
    return s.exec(
        select(IngestJob).where(IngestJob.query == query, col(IngestJob.status).in_(_ACTIVE))
    ).first()


def enqueue(query: str, kind: str = "ingest") -> dict:
    """Поставить скачивание в очередь. Тот же активный запрос — то же задание."""
    with _enqueue_lock, Session(engine) as s:
        existing = _active_by_query(s, query)
        if existing is not None:
            return _public(existing, deduplicated=True)
        active = len(s.exec(select(IngestJob.id).where(col(IngestJob.status).in_(_ACTIVE))).all())
        if active >= MAX_ACTIVE:
            raise QueueFull(f"в очереди уже {active} скачиваний — дождитесь окончания")
        t = now()
        row = IngestJob(id=uuid.uuid4().hex, kind=kind, query=query, created_at=t, updated_at=t)
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            # Та же ссылка встала в очередь параллельно (другой процесс на этой БД):
            # уникальный индекс отбил дубль — отдаём существующее задание.
            s.rollback()
            existing = _active_by_query(s, query)
            if existing is None:
                raise
            return _public(existing, deduplicated=True)
        s.refresh(row)
        snap = _public(row, deduplicated=False)
    log.info("задание %s поставлено в очередь: %s", snap["job_id"], mask(query))
    _wake.set()
    return snap


def get(job_id: str) -> dict | None:
    with Session(engine) as s:
        row = s.get(IngestJob, job_id)
        return _public(row) if row is not None else None


def list_jobs(limit: int = 20, recent_hours: int = 24) -> dict:
    """Задания для панели: все активные (по времени постановки) и завершённые за
    последние recent_hours (свежие первыми)."""
    limit = max(1, min(int(limit), 100))
    t = now()
    with Session(engine) as s:
        active = s.exec(
            select(IngestJob).where(col(IngestJob.status).in_(_ACTIVE)).order_by(col(IngestJob.created_at))
        ).all()
        recent = s.exec(
            select(IngestJob)
            .where(
                col(IngestJob.status).in_(("done", "error")),
                col(IngestJob.finished_at) >= t - timedelta(hours=recent_hours),
            )
            .order_by(col(IngestJob.finished_at).desc())
            .limit(limit)
        ).all()
        jobs = [_public(r) for r in active] + [_public(r) for r in recent]
    return {"active": len(active), "jobs": jobs[: max(limit, len(active))]}


def _claim() -> tuple[str, IngestJob] | None:
    """Взять следующее готовое задание. ("run", row) | ("stopped", row) | None."""
    t = now()
    with Session(engine) as s:
        cands = s.exec(
            select(IngestJob)
            .where(
                IngestJob.status == "queued",
                or_(col(IngestJob.not_before).is_(None), col(IngestJob.not_before) <= t),
            )
            .order_by(col(IngestJob.created_at))
            .limit(5)
        ).all()
        for cand in cands:
            job_id, query = cand.id, cand.query
            if cand.attempts >= cand.max_attempts:
                msg = (
                    f"прервано {cand.interruptions} раз(а) подряд — задание не возобновляется, "
                    "чтобы не устроить карусель рестартов; добавьте книгу заново"
                )
                res = s.execute(
                    update(IngestJob)
                    .where(col(IngestJob.id) == job_id, col(IngestJob.status) == "queued")
                    .values(status="error", finished_at=t, updated_at=t, error=msg)
                )
                s.commit()
                if res.rowcount == 1:
                    log.warning("задание %s остановлено: %s (%s)", job_id, msg, mask(query))
                    return "stopped", s.get(IngestJob, job_id)
                continue
            res = s.execute(
                update(IngestJob)
                .where(col(IngestJob.id) == job_id, col(IngestJob.status) == "queued")
                .values(
                    status="running",
                    attempts=IngestJob.attempts + 1,
                    worker_unit=WORKER_UNIT,
                    worker_boot=WORKER_BOOT,
                    started_at=t,
                    heartbeat_at=t,
                    updated_at=t,
                    finished_at=None,
                    error=None,
                )
            )
            s.commit()
            if res.rowcount == 1:
                row = s.get(IngestJob, job_id)
                s.refresh(row)
                s.expunge(row)
                return "run", row
        return None


def _should_retry(attempts: int, max_attempts: int) -> bool:
    """Осталась ли у задания попытка после ошибки скачивания.

    Правило простое: пока потраченных попыток меньше разрешённых — пробуем
    снова. Сбой источника часто разовый (serg/tasks#986: та же книга скачалась
    с повтора без единой правки кода), а человеку нажимать «Добавить» заново,
    не понимая, есть ли смысл, — плохой ответ.
    """
    return (attempts or 0) < (max_attempts or 0)


# Причины, которые по опыту ридера бывают РАЗОВЫМИ: их и только их имеет смысл
# повторять. Список белый намеренно — см. _is_retryable.
_RETRYABLE_MARKERS = (
    "epub не создан",      # FanFicFare промолчал (serg/tasks#986)
    "промолчал",
    "тайм-аут",
    "таймаут",
    "timeout",
    "сетевая ошибка",
    "недоступен",
)

# Источник ответил, но своей поломкой (5xx), а не отказом: «вернуло 503».
_SERVER_ERROR_RE = re.compile(r"(?:вернул[оа]?|код ответа|status)\s*5\d\d\b", re.I)


def _is_retryable(message: str | None) -> bool:
    """Стоит ли повторять скачивание с такой причиной отказа.

    Список БЕЛЫЙ, и это осознанно. «Story does not exist», 403 и «нет адаптера» —
    приговор: повтор через минуту ничего не изменит, зато человек вместо честной
    ошибки несколько минут смотрит на задание в очереди. Ошибиться в сторону
    «error» дешевле, чем гонять карусель повторов по приговору, поэтому
    незнакомая причина считается окончательной.
    """
    text = (message or "").lower()
    if not text:
        return False
    if any(m in text for m in _RETRYABLE_MARKERS):
        return True
    return bool(_SERVER_ERROR_RE.search(text))


def _finish_error(job_id: str, message: str) -> None:
    """Записать отказ: вернуть задание в очередь, пока есть попытки, иначе error."""
    t = now()
    with Session(engine) as s:
        row = s.get(IngestJob, job_id)
        if (
            row is not None
            and _is_retryable(message)
            and _should_retry(row.attempts, row.max_attempts)
        ):
            res = s.execute(
                update(IngestJob)
                .where(col(IngestJob.id) == job_id, col(IngestJob.status) == "running")
                .values(
                    status="queued",
                    finished_at=None,
                    updated_at=t,
                    not_before=t + ERROR_RETRY_DELAY,
                    error=mask(message)[:500],
                )
            )
            s.commit()
            if res.rowcount == 1:
                log.warning(
                    "задание %s: ошибка «%s», попытка %s из %s — повтор через %s с",
                    job_id, mask(message)[:200], row.attempts, row.max_attempts,
                    int(ERROR_RETRY_DELAY.total_seconds()),
                )
                return
        s.execute(
            update(IngestJob)
            .where(col(IngestJob.id) == job_id, col(IngestJob.status) == "running")
            .values(status="error", finished_at=t, updated_at=t, error=mask(message)[:500])
        )
        s.commit()


def process_one() -> bool:
    """Выполнить одно готовое задание в текущем потоке. False — делать нечего."""
    got = _claim()
    if got is None:
        return False
    action, row = got
    if action == "stopped":
        return True
    if row.interruptions:
        log.info(
            "возобновлено после рестарта: job=%s «%s», попытка %s из %s (прервано: %s)",
            row.id, row.title or mask(row.query), row.attempts, row.max_attempts, row.interrupted_by,
        )
    try:
        if row.kind != "ingest":
            raise ValueError(f"неизвестный вид задания: {row.kind}")
        from .ingest_service import run_ingest_job  # ленивый импорт: цикл через роутер

        run_ingest_job(row.id, row.query)
    except DownloaderError as e:
        # Причину с длительностью ingest_service уже записал warning'ом.
        _finish_error(row.id, str(e))
    except Exception as e:  # noqa: BLE001 — воркер не роняем, но и не молчим
        _log_failure("задание %s упало: %s", row.id, mask(row.query), exc=e)
        _finish_error(row.id, f"{type(e).__name__}: {e}")
    return True


def mark_shutdown() -> int:
    """Плановая остановка: свои running — обратно в очередь, попытка не сгорает."""
    t = now()
    with Session(engine) as s:
        rows = s.exec(
            select(IngestJob).where(IngestJob.status == "running", IngestJob.worker_boot == WORKER_BOOT)
        ).all()
        info = [(r.id, r.query) for r in rows]
        for r in rows:
            r.status = "queued"
            r.attempts = max(0, r.attempts - 1)
            r.interruptions += 1
            r.interrupted_by = "shutdown"
            r.not_before = None
            r.updated_at = t
            s.add(r)
        s.commit()
    for job_id, query in info:
        log.warning("задание %s прервано остановкой сервиса, продолжится после старта: %s", job_id, mask(query))
    return len(info)


def recover_on_startup() -> int:
    """Старт: свои running прошлого запуска — крэш; вернуть в очередь с отсрочкой."""
    t = now()
    with Session(engine) as s:
        rows = s.exec(
            select(IngestJob).where(
                IngestJob.status == "running",
                IngestJob.worker_unit == WORKER_UNIT,
                IngestJob.worker_boot != WORKER_BOOT,
            )
        ).all()
        info = [(r.id, r.query, r.attempts, r.max_attempts) for r in rows]
        for r in rows:
            r.status = "queued"
            r.interruptions += 1
            r.interrupted_by = "crash"
            r.not_before = t + CRASH_RETRY_DELAY
            r.updated_at = t
            s.add(r)
        purged = s.execute(
            delete(IngestJob).where(
                col(IngestJob.status).in_(("done", "error")),
                col(IngestJob.finished_at) < t - RETENTION,
            )
        ).rowcount
        s.commit()
    for job_id, query, attempts, max_attempts in info:
        log.warning(
            "задание %s прервано без штатной остановки (крэш/OOM/kill), возобновлю через %s с: "
            "запусков было %s из %s — %s",
            job_id, int(CRASH_RETRY_DELAY.total_seconds()), attempts, max_attempts, mask(query),
        )
    if purged:
        log.info("очередь скачиваний: удалено %s завершённых заданий старше %s дн.", purged, RETENTION.days)
    return len(info)


def _loop(gen: int) -> None:
    while not _stop.is_set() and gen == _generation:
        try:
            busy = process_one()
        except Exception as e:  # noqa: BLE001 — БД недоступна и т.п.: воркер живёт, след в логе
            _log_failure("воркер очереди скачиваний: сбой цикла", exc=e)
            busy = False
        if not busy:
            _wake.wait(POLL_S)
            _wake.clear()


def start_workers() -> None:
    global _generation
    if any(t.is_alive() for t in _threads):
        return
    _generation += 1
    _stop.clear()
    _threads.clear()
    for i in range(SLOTS):
        th = threading.Thread(target=_loop, args=(_generation,), daemon=True, name=f"ingest-worker-{i}")
        th.start()
        _threads.append(th)
    log.info("очередь скачиваний: %s воркера, %s", SLOTS, WORKER_UNIT)


def stop_workers(mark: bool = True) -> None:
    """Остановка пула. Идущее скачивание не ждём: оно дольше TimeoutStopSec, а
    задание честно вернётся в очередь (mark_shutdown) и продолжится после старта."""
    global _generation
    _generation += 1
    _stop.set()
    _wake.set()
    if mark:
        try:
            mark_shutdown()
        except Exception as e:  # noqa: BLE001 — БД недоступна на остановке: при старте это задание уйдёт как крэш
            _log_failure("очередь скачиваний: не удалось вернуть задания в очередь при остановке", exc=e)
    for th in _threads:
        th.join(timeout=1.0)
    _threads.clear()
