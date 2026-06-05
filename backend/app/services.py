"""Сервисные функции: регистрация скачанной книги как Work (хранилище + Calibre)."""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from ..calibre import client as calibre
from ..downloaders.base import DownloadResult
from .db.models import Work, utcnow
from .storage import import_file, sha1_of_file


def _push_readera(dest) -> None:
    """Best-effort: положить книгу в ReadEra/Books (Drive) — ReadEra Premium
    подтянет её на телефон, и doc_sha1 совпадёт с нашим (включает sync прогресса)."""
    try:
        from ..readera import gdrive
        gdrive.push_book(dest)
    except Exception:  # noqa: BLE001 — не критично для скачивания
        pass


def register_download(result: DownloadResult, session: Session) -> Work:
    """Импортировать скачанный файл в хранилище, создать/обновить Work, добавить
    в Calibre/ReadEra.

    Дедуп/обновление:
    - если по source_url уже есть Work — обновляем его (новый файл/sha1/число глав),
      сохраняя work_id (значит прогресс/мониторинг не теряются);
    - иначе дедуп по sha1 (тот же самый файл) — возвращаем существующий;
    - иначе создаём новый Work.
    """
    src = Path(result.file_path)
    sha1 = sha1_of_file(src)

    # Обновление по источнику (например, при докачке обновлённого фика).
    if result.source_url:
        same_src = session.exec(
            select(Work).where(Work.source_url == result.source_url)
        ).first()
        if same_src:
            if same_src.sha1 != sha1:
                dest, _ = import_file(src, sha1)
                same_src.file_path = str(dest)
                same_src.file_format = result.file_format
                same_src.sha1 = sha1
                same_src.chapters_count = result.num_chapters or same_src.chapters_count
                same_src.calibre_id = calibre.add_book(dest) or same_src.calibre_id
                _push_readera(dest)
            same_src.updated_at = utcnow()
            session.add(same_src)
            session.commit()
            session.refresh(same_src)
            return same_src

    existing = session.exec(select(Work).where(Work.sha1 == sha1)).first()
    if existing:
        # Тот же самый файл уже есть.
        existing.updated_at = utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    dest, _ = import_file(src, sha1)

    # Best-effort: добавить в Calibre (на VPS). Локально без Calibre -> None.
    calibre_id = calibre.add_book(dest)
    _push_readera(dest)

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
