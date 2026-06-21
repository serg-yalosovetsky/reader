"""Фоновая проверка обновлений: единый guard для HTTP-кнопки и планировщика.

Кнопка «↻ Обновления» раньше дёргала monitor.check_all синхронно — скрейп 38
фиклов с внешних сайтов не укладывался в дефолтный nginx proxy_read_timeout (60s)
и фронт ловил 504, хотя проверка в фоне доходила до конца. Теперь HTTP-эндпоинт
сразу отдаёт {status:"started"}, а реальная работа крутится в daemon-потоке;
фронт поллит /api/monitored/check/status.

In-memory _state корректен ТОЛЬКО при uvicorn --workers 1 (см. reader.service).
С ≥2 воркерами POST и status-поллинг попадут в разные процессы → поллер навсегда
увидит 'idle'. При масштабировании воркеров нужен общий store (БД/redis).

Один и тот же _lock сериализует ручной запуск (start) и плановый тик APScheduler
(run_blocking) — параллельных check_all не будет, значит нет двойных докачек и
конкуренции за SQLite.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlmodel import Session

from ..app.db.session import engine
from . import monitor

_lock = threading.Lock()
_state: dict = {
    "status": "idle",       # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
    "trigger": None,        # manual | scheduled
    "progress": {
        "current": 0,
        "total": 0,
        "current_title": "",
        "current_site": "",
        "updates_found": 0,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state() -> dict:
    """Снимок состояния для /status (копия — наружу не отдаём живой dict)."""
    return dict(_state)


def _run(trigger: str) -> None:
    _state["progress"].update(current=0, total=0, current_title="", current_site="", updates_found=0)

    def _progress_cb(current: int, total: int, title: str, site: str) -> None:
        _state["progress"].update(current=current, total=total,
                                  current_title=title, current_site=site)

    def _update_cb(updates_found: int) -> None:
        _state["progress"]["updates_found"] = updates_found

    try:
        with Session(engine) as session:
            res = monitor.check_all(session, auto_download=True,
                                    progress_cb=_progress_cb, update_cb=_update_cb)
        _state.update(status="done", finished_at=_now(), result=res, error=None)
    except Exception as e:  # noqa: BLE001 — фон, не роняем поток/планировщик
        _state.update(status="error", finished_at=_now(), error=str(e)[:300])


def start(trigger: str = "manual") -> dict:
    """Запустить проверку в фоне, если ещё не идёт. {started} либо {running}."""
    with _lock:
        if _state["status"] == "running":
            return {"status": "running", "started_at": _state["started_at"],
                    "trigger": _state["trigger"]}
        _state.update(status="running", started_at=_now(), finished_at=None,
                      result=None, error=None, trigger=trigger)
    threading.Thread(target=_run, args=(trigger,), daemon=True,
                     name="monitor-check").start()
    return {"status": "started", "trigger": trigger}


def run_blocking(trigger: str = "scheduled") -> dict:
    """Для APScheduler: выполнить в текущем (рабочем) потоке планировщика.
    Если ручная проверка уже идёт — пропустить тик (не плодим параллель)."""
    with _lock:
        if _state["status"] == "running":
            return {"status": "skipped", "reason": "already running"}
        _state.update(status="running", started_at=_now(), finished_at=None,
                      result=None, error=None, trigger=trigger)
    _run(trigger)
    return state()
