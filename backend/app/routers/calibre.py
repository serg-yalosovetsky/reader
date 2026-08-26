"""Роутер Calibre: каталог, обложки и заведение книги-ссылки в читалку.

Боевой режим — HTTP/OPDS (calibre-web по Tailscale). Книга НЕ копируется: в БД
кладётся Work-ссылка (site=calibre, calibre_id, file_path=""), файл тянется по
требованию при открытии (routers/reader.py fetch-on-open). Дублирования нет.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ...calibre import client as calibre
from ...calibre import sync as calibre_sync
from ..db.models import Work, utcnow
from ..db.session import get_session

router = APIRouter(prefix="/api/calibre", tags=["calibre"])


@router.get("/status")
def status() -> dict:
    return {
        "configured": calibre.is_configured(),
        "mode": "http" if calibre.http_mode() else "local",
    }


# Фронту от каталога нужны только эти поля (заголовок карточки, автор,
# обложка, импорт по id). Полная запись тянет ещё и description — на 1400
# книг это больше мегабайта, который никто не читает.
_LIST_FIELDS = ("calibre_id", "title", "authors", "has_cover", "updated")


@router.get("/books")
def books() -> list[dict]:
    """Список книг Calibre (OPDS в боевом режиме, metadata.db — локально)."""
    return [
        {k: b.get(k) for k in _LIST_FIELDS} for b in calibre.list_books()
    ]


@router.get("/{calibre_id}/cover")
def calibre_cover(calibre_id: int):
    """Обложка книги Calibre. HTTP — проксируем /opds/cover; локально — cover.jpg."""
    if calibre.http_mode():
        data = calibre.cover_bytes(calibre_id)
        if not data:
            raise HTTPException(404, "обложки нет")
        return Response(content=data, media_type="image/jpeg")
    # локальный режим
    lib = calibre._library()
    if not lib or not (lib / "metadata.db").exists():
        raise HTTPException(404, "Calibre не настроен")
    con = sqlite3.connect(f"file:{lib / 'metadata.db'}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT path, has_cover FROM books WHERE id=?", (calibre_id,)
        ).fetchone()
    finally:
        con.close()
    if not row or not row[1]:
        raise HTTPException(404, "обложки нет")
    cover = lib / row[0] / "cover.jpg"
    if not cover.exists():
        raise HTTPException(404, "файл обложки не найден")
    return FileResponse(cover, media_type="image/jpeg")


@router.post("/import/{calibre_id}")
def import_book(calibre_id: int, session: Session = Depends(get_session)) -> Work:
    """Завести книгу Calibre в читалку как ССЫЛКУ (без копии файла). Файл придёт
    по требованию при открытии. Идемпотентно по calibre_id."""
    existing = session.exec(select(Work).where(Work.calibre_id == calibre_id)).first()
    if existing:
        return existing

    def _find(force: bool = False):
        return next(
            (b for b in calibre.list_books(force=force) if b["calibre_id"] == calibre_id),
            None,
        )

    # Промах может означать не «книги нет», а «кэш каталога устарел» —
    # книгу добавили в Calibre только что. Перепроверяем по свежему каталогу.
    meta = _find() or _find(force=True)
    if not meta:
        raise HTTPException(404, "книга в Calibre не найдена")
    fmt = calibre.best_format(meta["formats"])
    if not fmt:
        raise HTTPException(415, "у книги нет скачиваемого формата")

    import json

    work = Work(
        title=meta.get("title", ""),
        author=meta.get("authors", ""),
        site="calibre",
        calibre_id=calibre_id,
        file_path="",  # ссылка: файл тянется при открытии
        file_format=fmt,
        sha1="",
        description=meta.get("description", "") or "",
        genres=json.dumps(meta.get("tags") or [], ensure_ascii=False),
        meta_synced=True,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(work)
    session.commit()
    session.refresh(work)
    return work


@router.post("/sync")
def sync(session: Session = Depends(get_session)) -> dict:
    """Пере-синк всего каталога Calibre в Work-ссылки (upsert по calibre_id)."""
    return calibre_sync.sync_catalog(session)


@router.post("/migrate")
def migrate(dry_run: bool = True, session: Session = Depends(get_session)) -> dict:
    """Перевести уже загруженные локальные копии на ссылки Calibre (dedup).
    dry_run=1 — только отчёт, ничего не меняет."""
    return calibre_sync.migrate_local_to_refs(session, dry_run=dry_run)
