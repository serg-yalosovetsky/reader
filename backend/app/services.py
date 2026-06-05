"""Сервисные функции: регистрация скачанной книги как Work (хранилище + Calibre).

Дедуп: одна книга, скачанная из разных источников/прогонов, не плодит карточки —
совпадение по source_url ИЛИ по нормализованным (название, автор); при совпадении
оставляем более полный файл (по размеру).
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlmodel import Session, select

from ..calibre import client as calibre
from ..downloaders.base import DownloadResult
from . import covers
from .db.models import Work, utcnow
from .storage import import_file, sha1_of_file


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'["“”«»\'`]', "", (s or "")).strip().lower())


def _push_readera(dest) -> None:
    try:
        from ..readera import gdrive
        gdrive.push_book(dest)
    except Exception:  # noqa: BLE001
        pass


def _apply_file(work: Work, dest: Path, result: DownloadResult, sha1: str) -> None:
    """Прописать в Work новый файл книги + Calibre/ReadEra/обложка."""
    work.file_path = str(dest)
    work.file_format = result.file_format
    work.sha1 = sha1
    if result.num_chapters:
        work.chapters_count = result.num_chapters
    work.calibre_id = calibre.add_book(dest) or work.calibre_id
    cover = covers.extract_cover(dest, result.file_format, sha1)
    if not cover and result.source_url:
        cover = covers.fetch_source_cover(result.source_url, sha1)
    if cover:
        work.cover_path = str(cover)
    _push_readera(dest)


def _find_existing(session: Session, result: DownloadResult) -> Work | None:
    if result.source_url:
        w = session.exec(select(Work).where(Work.source_url == result.source_url)).first()
        if w:
            return w
    title_n = _norm(result.title)
    if title_n:
        for w in session.exec(select(Work)).all():
            if _norm(w.title) == title_n and _norm(w.author) == _norm(result.author):
                return w
    return None


def register_download(result: DownloadResult, session: Session) -> Work:
    src = Path(result.file_path)
    sha1 = sha1_of_file(src)
    new_size = src.stat().st_size

    existing = _find_existing(session, result)
    if existing:
        if existing.sha1 != sha1:
            # Заменяем файл только если новый «полнее» (крупнее) — берём полную книгу.
            cur_size = Path(existing.file_path).stat().st_size if existing.file_path and Path(existing.file_path).exists() else 0
            if new_size >= cur_size:
                dest, _ = import_file(src, sha1)
                _apply_file(existing, dest, result, sha1)
        if result.source_url and not existing.source_url:
            existing.source_url = result.source_url
        existing.updated_at = utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    dest, _ = import_file(src, sha1)
    work = Work(
        title=result.title or dest.stem, author=result.author, site=result.site,
        source_url=result.source_url, chapters_count=result.num_chapters,
        created_at=utcnow(), updated_at=utcnow(),
    )
    _apply_file(work, dest, result, sha1)
    session.add(work)
    session.commit()
    session.refresh(work)
    return work
