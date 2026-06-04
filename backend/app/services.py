"""Сервисные функции: регистрация скачанной книги как Work (хранилище + Calibre)."""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from ..calibre import client as calibre
from ..downloaders.base import DownloadResult
from .db.models import Work, utcnow
from .storage import import_file, sha1_of_file


def register_download(result: DownloadResult, session: Session) -> Work:
    """Импортировать скачанный файл в хранилище, дедуплицировать по SHA-1,
    создать/обновить Work и попытаться добавить в Calibre."""
    src = Path(result.file_path)
    sha1 = sha1_of_file(src)

    existing = session.exec(select(Work).where(Work.sha1 == sha1)).first()
    if existing:
        # Книга уже есть — обновим источник/метаданные при необходимости.
        existing.updated_at = utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    dest, _ = import_file(src, sha1)

    # Best-effort: добавить в Calibre (на VPS). Локально без Calibre -> None.
    calibre_id = calibre.add_book(dest)

    work = Work(
        title=result.title or dest.stem,
        author=result.author,
        site=result.site,
        source_url=result.source_url,
        file_path=str(dest),
        file_format=result.file_format,
        sha1=sha1,
        calibre_id=calibre_id,
        chapters_count=result.num_chapters,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    session.add(work)
    session.commit()
    session.refresh(work)
    return work
