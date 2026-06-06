"""Роутер библиотеки: список произведений, карточка, загрузка файла вручную."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

import os

from .. import covers
from ..db.models import Monitored, Progress, Work, utcnow
from ..db.session import get_session
from ..services import _norm
from ..storage import detect_format, import_file, sha1_of_file

router = APIRouter(prefix="/api/library", tags=["library"])


def _fsize(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


@router.post("/maintenance")
def maintenance(session: Session = Depends(get_session)) -> dict:
    """Убрать дубликаты книг (оставить самый полный файл), подчистить мониторинг,
    добэкафиллить обложки."""
    # 1) Группировка по (название, автор).
    groups: dict[tuple, list[Work]] = {}
    for w in session.exec(select(Work)).all():
        groups.setdefault((_norm(w.title), _norm(w.author)), []).append(w)

    removed_works = 0
    for ws in groups.values():
        if len(ws) <= 1:
            continue
        ws.sort(key=lambda w: _fsize(w.file_path), reverse=True)  # самый полный — первым
        keep = ws[0]
        for dup in ws[1:]:
            for m in session.exec(select(Monitored).where(Monitored.work_id == dup.id)).all():
                m.work_id = keep.id
                session.add(m)
            for p in session.exec(select(Progress).where(Progress.work_id == dup.id)).all():
                session.delete(p)
            if dup.file_path and dup.file_path != keep.file_path:
                try:
                    os.remove(dup.file_path)
                except OSError:
                    pass
            session.delete(dup)
            removed_works += 1
    session.commit()

    # 2) Дедуп мониторинга по work_id / source_url.
    seen: set = set()
    removed_mon = 0
    for m in session.exec(select(Monitored)).all():
        key = ("w", m.work_id) if m.work_id else ("u", m.source_url)
        if key in seen:
            session.delete(m)
            removed_mon += 1
        else:
            seen.add(key)
    session.commit()

    # 3) Бэкафилл обложек.
    added_covers = 0
    for w in session.exec(select(Work)).all():
        if w.cover_path and os.path.exists(w.cover_path):
            continue
        c = None
        if w.file_path and os.path.exists(w.file_path):
            c = covers.extract_cover(w.file_path, w.file_format, w.sha1)
        if not c and w.source_url:
            c = covers.fetch_source_cover(w.source_url, w.sha1)
        if c:
            w.cover_path = str(c)
            session.add(w)
            added_covers += 1
    session.commit()
    return {"removed_duplicates": removed_works, "removed_monitored": removed_mon,
            "covers_added": added_covers}


@router.get("")
def list_works(session: Session = Depends(get_session)) -> list[Work]:
    """Все произведения, новые сверху."""
    return list(session.exec(select(Work).order_by(Work.updated_at.desc())).all())



def _do_refresh_covers() -> None:
    """Фоновое обновление обложек — запускается из refresh_covers."""
    from ...downloaders import authortoday as _at
    from ..db.session import get_session as _gs
    from urllib.parse import urlparse
    import re as _re
    import httpx as _httpx

    _ELIGIBLE_HOSTS = ("ficbook.net", "readli.net", "searchfloor.org", "fanfics.me")

    def _host_ok(url: str) -> bool:
        h = (urlparse(url).hostname or "").lower()
        return any(h.endswith(e) for e in _ELIGIBLE_HOSTS)

    def _author_match(our: str, at_author: str) -> bool:
        if not our or not at_author:
            return False
        our_words = {w.lower().strip(".,") for w in our.split() if len(w) > 2}
        at_words  = {w.lower().strip(".,") for w in at_author.split() if len(w) > 2}
        return bool(our_words & at_words)

    def _at_author(at_url: str) -> str:
        try:
            r = _httpx.get(at_url, timeout=10, follow_redirects=True,
                           headers={"User-Agent": "Mozilla/5.0"})
            _pat = "itemprop=['\"{0,1}author['\"{0,1}[^>]*>([^<]{2,60})<"
            m = _re.search(_pat, r.text)
            if not m:
                m = _re.search(r"book-authors[^>]*>.*?href=[^>]+>([^<]{2,60})<", r.text, _re.S)
            return m.group(1).strip() if m else ""
        except Exception:
            return ""

    for session in _gs():
        works = session.exec(select(Work)).all()
        for w in works:
            if not w.title or not w.source_url or not _host_ok(w.source_url):
                continue
            try:
                at_url = _at.search_work(w.title, w.author or "")
                if not at_url:
                    continue
                at_author = _at_author(at_url)
                if not _author_match(w.author or "", at_author):
                    continue
                img_bytes = covers.fetch_cover_bytes(at_url)
                if not img_bytes or len(img_bytes) < 5000:
                    continue
                new_path = covers.save_cover_bytes(img_bytes, w.sha1)
                if new_path:
                    w.cover_path = str(new_path)
                    session.add(w)
                    session.commit()
            except Exception:  # noqa: BLE001
                pass


@router.post("/refresh-covers")
def refresh_covers(background_tasks: BackgroundTasks) -> dict:
    """Запускает обновление обложек с author.today в фоне, возвращает сразу."""
    background_tasks.add_task(_do_refresh_covers)
    return {"status": "started"}

@router.get("/{work_id}")
def get_work(work_id: int, session: Session = Depends(get_session)) -> Work:
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    return work


@router.post("/upload")
async def upload_book(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> Work:
    """Ручная загрузка EPUB/FB2 (полезно на этапе 1 и как фоллбэк)."""
    fmt = detect_format(file.filename or "")
    if not fmt:
        raise HTTPException(400, "поддерживаются только .epub, .fb2 и .pdf")

    # Сохраняем во временный файл, считаем SHA-1, импортируем в хранилище.
    suffix = Path(file.filename or "").suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
    try:
        sha1 = sha1_of_file(tmp_path)
        # Дедуп: если книга с таким SHA-1 уже есть — вернуть её.
        existing = session.exec(select(Work).where(Work.sha1 == sha1)).first()
        if existing:
            return existing
        dest, _ = import_file(tmp_path, sha1)
    finally:
        tmp_path.unlink(missing_ok=True)

    work = Work(
        title=Path(file.filename or "Без названия").stem,
        site="upload",
        file_path=str(dest),
        file_format=fmt,
        sha1=sha1,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(work)
    session.commit()
    session.refresh(work)
    return work


@router.delete("/{work_id}")
def delete_work(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Удалить книгу из библиотеки (файл + БД)."""
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    for p in session.exec(select(Progress).where(Progress.work_id == work_id)).all():
        session.delete(p)
    for m in session.exec(select(Monitored).where(Monitored.work_id == work_id)).all():
        session.delete(m)
    if work.file_path:
        try: os.remove(work.file_path)
        except OSError: pass
    if work.cover_path:
        try: os.remove(work.cover_path)
        except OSError: pass
    session.delete(work)
    session.commit()
    return {"ok": True}
