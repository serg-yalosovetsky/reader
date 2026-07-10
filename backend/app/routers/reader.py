"""Роутер чтения: отдаёт файл книги для рендера во foliate-js на клиенте."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from .. import config, covers, imagegen
from ..db.models import Work
from ..db.session import get_session

router = APIRouter(prefix="/api/reader", tags=["reader"])

log = logging.getLogger("reader.cover")

_MEDIA = {
    "epub": "application/epub+zip",
    "fb2": "application/x-fictionbook+xml",
    "pdf": "application/pdf",
}

# Дедуп одновременной генерации одной и той же обложки (несколько <img> одной
# книги на странице / параллельные вкладки не должны плодить генерации).
_gen_locks: dict[int, threading.Lock] = {}
_gen_registry_lock = threading.Lock()

# ВАЖНО: генерация НЕ должна блокировать отдачу обложек. Ленивую генерацию
# уводим в фоновый пул (макс 2 разом), чтобы медленный Pollinations (1-120с) не
# забивал потоки uvicorn — иначе даже книги с готовой обложкой висят. Запрос
# /cover отдаёт готовое мгновенно либо 404 (фолбэк-текст), а обложка появляется
# на следующем заходе.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="covergen"
)
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _lock_for(work_id: int) -> threading.Lock:
    with _gen_registry_lock:
        lk = _gen_locks.get(work_id)
        if lk is None:
            lk = _gen_locks[work_id] = threading.Lock()
        return lk


def _bg_generate(work_id: int) -> None:
    """Фоновая ленивая генерация (в пуле): своя сессия, снимает in-flight флаг."""
    try:
        from ..db.session import engine

        with Session(engine) as session:
            _generate_and_persist(work_id, session, force=False)
    except Exception as e:  # noqa: BLE001
        log.warning("Фоновая генерация обложки work=%s упала: %s", work_id, e)
    finally:
        with _inflight_lock:
            _inflight.discard(work_id)


def _schedule_generation(work: Work) -> None:
    """Поставить ленивую генерацию в фоновую очередь (с дедупом)."""
    if not (
        config.IMAGE_GEN_ENABLED and work.title and work.cover_source != "gen_failed"
    ):
        return
    with _inflight_lock:
        if work.id in _inflight:
            return
        _inflight.add(work.id)
    _EXECUTOR.submit(_bg_generate, work.id)


def _schedule_generation_by_id(work_id: int) -> None:
    """Как _schedule_generation, но по id — короткой сессией (коннект БД не
    держится дольше чтения флагов)."""
    from ..db.session import engine

    with Session(engine) as s:
        work = s.get(Work, work_id)
        if work:
            _schedule_generation(work)


def _usable_cover(work: Work) -> Path | None:
    if work and work.cover_path:
        p = Path(work.cover_path)
        if p.exists():
            return p
    return None


def _meta_of(work: Work) -> dict:
    return {
        "title": work.title,
        "author": work.author,
        "genres": work.genres,
        "description": work.description,
        "fandom": work.fandom,
        "rating": work.rating,
        "cover_brief": work.cover_brief,
    }


def _ensure_brief(work: Work) -> None:
    """Заполнить work.cover_brief через Ollama, если ещё пусто (кеш)."""
    if config.BRIEF_ENABLED and not (work.cover_brief or "").strip():
        brief = imagegen.summarize(_meta_of(work))
        if brief:
            work.cover_brief = brief


def _generate_and_persist(
    work_id: int, session: Session, *, force: bool, provider: str | None = None
) -> Path | None:
    """Сгенерировать обложку ИИ под локом и записать результат в БД."""
    lk = _lock_for(work_id)
    with lk:
        work = session.get(Work, work_id)
        if not work:
            return None
        existing = _usable_cover(work)  # другой поток мог успеть, пока ждали лок
        if existing and not force:
            return existing
        _ensure_brief(work)  # арт-бриф (Ollama) → в промпт; кешируется в work
        salt = (
            f"-{int(threading.current_thread().ident or 0) & 0xFFFF}" if force else ""
        )
        try:
            path = covers.generate_cover(
                _meta_of(work), work.sha1, salt=salt, provider=provider
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Генерация обложки work=%s упала: %s", work_id, e)
            path = None
        if path:
            work.cover_path = str(path)
            work.cover_source = "generated"
        else:
            work.cover_source = "gen_failed"
        session.add(work)
        session.commit()
        return path


def _serve(path: Path, sha1: str) -> FileResponse:
    mtime = int(path.stat().st_mtime)
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["ETag"] = f'"{sha1}-{mtime}"'
    return resp


@router.get("/{work_id}/file")
def get_book_file(work_id: int) -> FileResponse:
    """Бинарь книги (EPUB/FB2). foliate-js грузит и рендерит его на клиенте.

    Коннект БД держится только на чтение метаданных; сама докачка из Calibre
    (до 180с) идёт БЕЗ занятого коннекта — иначе параллельные открытия забивали
    пул и книга «висла, срабатывала со второй попытки»."""
    from ..db.session import engine

    with Session(engine) as session:
        work = session.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        is_calibre_link = work.site == "calibre" and not work.file_path
        file_path = work.file_path
        file_format = work.file_format

    if is_calibre_link:
        # Книга-ссылка: тянем файл из Calibre по требованию в вытесняемый кэш.
        from ...calibre import sync as csync

        path = csync.ensure_cached(work_id)
        if not path:
            raise HTTPException(502, "не удалось получить файл из Calibre")
    else:
        if not file_path:
            raise HTTPException(404, "файл книги не найден")
        path = Path(file_path)
        if not path.exists():
            raise HTTPException(410, "файл книги отсутствует на диске")
    media = _MEDIA.get(file_format, "application/octet-stream")
    resp = FileResponse(path, media_type=media, filename=path.name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/{work_id}/cover")
@router.head("/{work_id}/cover")
def get_cover(work_id: int) -> FileResponse:
    """Обложка книги. Если её нет — пробуем сгенерировать ИИ (лениво, 1 раз).
    Иначе 404 → фронт рисует текстовую заглушку.

    Коннект БД держится только на чтение метаданных; сетевой запрос обложки из
    Calibre идёт без него (см. get_book_file)."""
    from ..db.session import engine

    with Session(engine) as session:
        work = session.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        path = _usable_cover(work)
        if path:
            return _serve(path, work.sha1)
        is_calibre = work.site == "calibre" and bool(work.calibre_id)
        sha_fallback = work.sha1 or (f"cal{work.calibre_id}" if work.calibre_id else "")

    # Книга Calibre: берём настоящую обложку из Calibre по требованию (кешируем).
    if is_calibre:
        from ...calibre import sync as csync

        cpath = csync.ensure_cover(work_id)
        if cpath:
            return _serve(cpath, sha_fallback)

    # Обложки нет — НЕ блокируем запрос генерацией: ставим её в фон (дедуп,
    # gen_failed не повторяем) и сразу отдаём 404 → фронт рисует текст-заглушку.
    # Обложка появится при следующем открытии библиотеки.
    _schedule_generation_by_id(work_id)
    raise HTTPException(404, "обложки нет")


@router.post("/{work_id}/cover/generate")
def regenerate_cover(
    work_id: int,
    force: bool = True,
    provider: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Принудительно сгенерировать/переснять обложку ИИ (кнопка на странице
    книги). force=1 игнорит существующую/gen_failed и рисует заново. provider=comfy
    — явно попросить FLUX (если ComfyUI доступен)."""
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "книги нет")
    path = _generate_and_persist(work_id, session, force=force, provider=provider)
    if not path:
        raise HTTPException(502, "не удалось сгенерировать обложку")
    return {
        "ok": True,
        "cover_v": int(path.stat().st_mtime),
        "cover_source": "generated",
    }
