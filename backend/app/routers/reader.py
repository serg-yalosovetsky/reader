"""Роутер чтения: отдаёт файл книги для рендера во foliate-js на клиенте."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from .. import config, covers
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


def _lock_for(work_id: int) -> threading.Lock:
    with _gen_registry_lock:
        lk = _gen_locks.get(work_id)
        if lk is None:
            lk = _gen_locks[work_id] = threading.Lock()
        return lk


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
    }


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
def get_book_file(
    work_id: int, session: Session = Depends(get_session)
) -> FileResponse:
    """Бинарь книги (EPUB/FB2). foliate-js грузит и рендерит его на клиенте."""
    work = session.get(Work, work_id)
    if not work or not work.file_path:
        raise HTTPException(404, "файл книги не найден")
    path = Path(work.file_path)
    if not path.exists():
        raise HTTPException(410, "файл книги отсутствует на диске")
    media = _MEDIA.get(work.file_format, "application/octet-stream")
    resp = FileResponse(path, media_type=media, filename=path.name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/{work_id}/cover")
@router.head("/{work_id}/cover")
def get_cover(work_id: int, session: Session = Depends(get_session)) -> FileResponse:
    """Обложка книги. Если её нет — пробуем сгенерировать ИИ (лениво, 1 раз).
    Иначе 404 → фронт рисует текстовую заглушку."""
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "книги нет")

    path = _usable_cover(work)
    if path:
        return _serve(path, work.sha1)

    # Ленивая генерация: только для книг с названием, один раз (gen_failed
    # блокирует авто-повтор — ручная ре-генерация через POST .../generate?force=1).
    if config.IMAGE_GEN_ENABLED and work.title and work.cover_source != "gen_failed":
        path = _generate_and_persist(work_id, session, force=False)
        if path:
            return _serve(path, work.sha1)

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
