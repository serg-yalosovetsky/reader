"""Фоновые задачи импорта веб-статей (поиск частей серии и сборка книги).

Зачем фон: 23 статьи с картинками качаются минутами, а nginx рвёт запрос по
proxy_read_timeout (60s) — как было с проверкой обновлений (см. accounts/check_job).
HTTP-эндпоинт сразу отдаёт {status:"started"}, фронт поллит /api/ingest/web/status.

In-memory состояние корректно ТОЛЬКО при uvicorn --workers 1 (так и запущен
reader.service). Одновременно выполняется одна задача: и поиск частей, и сборка
ходят на один и тот же сайт, параллелить их незачем.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlmodel import Session

from ..downloaders import webarticle
from ..downloaders.base import DownloaderError
from .db.session import engine
from .services import register_download

_lock = threading.Lock()
_state: dict = {
    "status": "idle",       # idle | running | done | error
    "kind": None,           # discover | build
    "started_at": None,
    "finished_at": None,
    "progress": {"current": 0, "total": 0, "url": ""},
    "result": None,
    "error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state() -> dict:
    snap = dict(_state)
    snap["progress"] = dict(_state["progress"])
    return snap


def _begin(kind: str) -> dict | None:
    """Занять слот. Вернёт состояние-отказ, если задача уже идёт."""
    with _lock:
        if _state["status"] == "running":
            return {"status": "running", "kind": _state["kind"],
                    "started_at": _state["started_at"]}
        _state.update(status="running", kind=kind, started_at=_now(),
                      finished_at=None, result=None, error=None)
        _state["progress"].update(current=0, total=0, url="")
    return None


def _finish(result: dict | None = None, error: str | None = None) -> None:
    _state.update(
        status="error" if error else "done",
        finished_at=_now(),
        result=result,
        error=error,
    )


def start_discover(url: str) -> dict:
    """Найти остальные части серии по одной ссылке (в фоне)."""
    busy = _begin("discover")
    if busy:
        return busy

    def _run() -> None:
        try:
            _state["progress"].update(total=1, current=0, url=url)
            res = webarticle.discover_parts(url)
            _state["progress"].update(current=1, total=1)
            _finish(result=res)
        except DownloaderError as e:
            _finish(error=str(e)[:300])
        except Exception as e:  # noqa: BLE001 — фон, поток не роняем
            _finish(error=f"{type(e).__name__}: {e}"[:300])

    threading.Thread(target=_run, daemon=True, name="web-discover").start()
    return {"status": "started", "kind": "discover"}


def start_build(urls: list[str], title: str = "", author: str = "") -> dict:
    """Собрать книгу из списка ссылок и зарегистрировать её в библиотеке."""
    busy = _begin("build")
    if busy:
        return busy

    def _run() -> None:
        def _progress(current: int, total: int, url: str) -> None:
            _state["progress"].update(current=current, total=total, url=url)

        try:
            result = webarticle.build_book(urls, title=title, author=author,
                                           progress_cb=_progress)
            with Session(engine) as session:
                work = register_download(result, session)
                extra = result.extra or {}
                _finish(result={
                    "work_id": work.id,
                    "title": work.title,
                    "author": work.author,
                    "chapters": work.chapters_count,
                    "images": extra.get("images", 0),
                    "images_skipped": extra.get("images_skipped", 0),
                    "parts": len(extra.get("web_parts") or []),
                    "warnings": (extra.get("warnings") or [])[:10],
                })
        except DownloaderError as e:
            _finish(error=str(e)[:300])
        except Exception as e:  # noqa: BLE001
            _finish(error=f"{type(e).__name__}: {e}"[:300])

    threading.Thread(target=_run, daemon=True, name="web-build").start()
    return {"status": "started", "kind": "build", "total": len(urls)}
