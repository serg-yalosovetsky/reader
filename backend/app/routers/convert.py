"""Роутер конвертации книги в EPUB (PDF → EPUB) и её статуса.

Конвертация идёт в фоновом пуле: ebook-convert на толстом PDF занимает от секунд
до минут, держать на нём HTTP-запрос (и коннект БД) нельзя — на этом уже горели
обложки и докачка из Calibre. Фронт стартует конвертацию и опрашивает статус.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlmodel import Session

from .. import convert
from ..db.models import Work
from ..db.session import engine

router = APIRouter(prefix="/api/reader", tags=["convert"])
log = logging.getLogger("reader.convert")

# Один воркер: ebook-convert жрёт CPU, параллельные конвертации на VPS душат
# uvicorn и друг друга. Очередь важнее скорости.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="convert"
)
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _source_file(work_id: int) -> tuple[Path | None, str, str, str]:
    """(путь к оригиналу, sha1, title, author). Книгу-ссылку Calibre докачиваем."""
    with Session(engine) as s:
        work = s.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        file_path = work.file_path
        sha1 = work.sha1
        title, author = work.title, work.author
        is_calibre_link = work.site == "calibre" and not work.file_path
        calibre_id = work.calibre_id

    if is_calibre_link:
        from ...calibre import sync as csync

        path = csync.ensure_cached(work_id)
        if not sha1:
            sha1 = f"cal{calibre_id}"
    else:
        path = Path(file_path) if file_path else None
        if path and not path.exists():
            path = None
    if not sha1:
        sha1 = f"w{work_id}"
    return path, sha1, title, author


def _set_state(work_id: int, **fields) -> None:
    with Session(engine) as s:
        work = s.get(Work, work_id)
        if not work:
            return
        for k, v in fields.items():
            setattr(work, k, v)
        s.add(work)
        s.commit()


def _run(work_id: int) -> None:
    """Фоновая конвертация: тянет оригинал, зовёт calibre, пишет результат в БД."""
    try:
        src, sha1, title, author = _source_file(work_id)
        if not src:
            _set_state(
                work_id,
                converted_status="failed",
                converted_error="файл книги недоступен",
            )
            return
        dest = convert.convert_to_epub(src, sha1, title=title, author=author)
        _set_state(
            work_id,
            converted_path=str(dest),
            converted_status="ready",
            converted_error="",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Конвертация work=%s не удалась: %s", work_id, e)
        _set_state(work_id, converted_status="failed", converted_error=str(e)[:500])
    finally:
        with _inflight_lock:
            _inflight.discard(work_id)


def _status_of(work: Work) -> dict:
    ready = bool(work.converted_path and Path(work.converted_path).exists())
    status = work.converted_status or ""
    if status == "ready" and not ready:
        status = ""  # файл удалили (чистка диска) — покажем как «не конвертировано»
    with _inflight_lock:
        if work.id in _inflight and status != "ready":
            status = "pending"
    return {
        "status": status,
        "ready": ready and status == "ready",
        "error": work.converted_error or "",
        "source_format": work.file_format or "",
        "convertible": convert.is_convertible(work.file_format)
        and convert.available(),
    }


@router.get("/{work_id}/convert")
def convert_status(work_id: int) -> dict:
    """Состояние EPUB-версии книги: "" | pending | ready | failed."""
    with Session(engine) as s:
        work = s.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        return _status_of(work)


@router.post("/{work_id}/convert")
def start_convert(work_id: int, force: bool = False) -> dict:
    """Запустить конвертацию в EPUB (идемпотентно; force — пересобрать заново)."""
    with Session(engine) as s:
        work = s.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        if not convert.available():
            raise HTTPException(501, "конвертер calibre (ebook-convert) не установлен")
        if not convert.is_convertible(work.file_format):
            raise HTTPException(
                400, f"формат {work.file_format or '?'} конвертировать незачем"
            )
        st = _status_of(work)
        if st["ready"] and not force:
            return st
        work.converted_status = "pending"
        work.converted_error = ""
        if force:
            work.converted_path = ""
        s.add(work)
        s.commit()

    with _inflight_lock:
        if work_id not in _inflight:
            _inflight.add(work_id)
            _EXECUTOR.submit(_run, work_id)
    return {
        "status": "pending",
        "ready": False,
        "error": "",
        "source_format": st["source_format"],
        "convertible": True,
    }
