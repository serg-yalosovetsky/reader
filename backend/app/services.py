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
    cover_src = "embedded" if cover else ""
    if not cover and result.file_format == "epub":
        desc = covers._epub_description(dest)
        if desc:
            cover = covers.cover_from_description(desc, sha1)
            cover_src = "description" if cover else ""
    if not cover and result.source_url:
        cover = covers.fetch_source_cover(
            result.source_url, sha1, result.title, result.author
        )
        cover_src = "source" if cover else ""
    if cover:
        work.cover_path = str(cover)
        work.cover_source = cover_src
    # Метаданные (описание, жанры/метки, статус, рейтинг) из свежего файла книги
    # (epub-opf или fb2 <title-info>).
    from . import bookmeta

    meta = bookmeta.extract_meta(dest, result.file_format)
    # Адаптеры (author.today) могут дать поля, которых нет в файле.
    for k in ("genres", "rating", "status", "characters", "fandom", "words", "series", "series_index"):
        if not meta.get(k) and result.extra.get(k):
            meta[k] = result.extra[k]
    if meta:
        bookmeta.apply_meta(work, meta, overwrite=True)
    _push_readera(dest)


def _find_existing(session: Session, result: DownloadResult) -> Work | None:
    if result.source_url:
        w = session.exec(
            select(Work).where(Work.source_url == result.source_url)
        ).first()
        if w:
            return w
    from .book_identity import same_book, work_descriptor

    ex = result.extra or {}
    rd = {
        "title": result.title or "",
        "author": result.author or "",
        "series": ex.get("series", ""),
        "series_index": ex.get("series_index", 0),
        "annotation": ex.get("annotation", ""),
    }
    if _norm(result.title):
        for w in session.exec(select(Work)).all():
            if same_book(rd, work_descriptor(w)):
                return w
    return None


def _richness(path, fmt: str) -> int:
    """Длина извлекаемого ТЕКСТА книги (символы) — мера полноты для сравнения зеркал."""
    import re
    import zipfile

    try:
        p = str(path)
        if (fmt or "").lower() == "epub" or p.lower().endswith(".epub"):
            z = zipfile.ZipFile(p)
            parts = []
            for n in z.namelist():
                if n.lower().endswith((".xhtml", ".html", ".htm")):
                    parts.append(
                        re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore"))
                    )
            return len(" ".join(parts))
        with open(p, encoding="utf-8", errors="ignore") as fh:
            return len(re.sub(r"<[^>]+>", " ", fh.read()))
    except Exception:  # noqa: BLE001
        try:
            return Path(path).stat().st_size
        except Exception:  # noqa: BLE001
            return 0


def register_download(result: DownloadResult, session: Session) -> Work:
    src = Path(result.file_path)
    sha1 = sha1_of_file(src)

    existing = _find_existing(session, result)
    if existing:
        if existing.sha1 != sha1:
            # Заменяем файл только если новый «полнее» — по длине извлекаемого ТЕКСТА
            # (байты ненадёжны: epub меньше fb2 при равном/большем контенте).
            cur_rich = (
                _richness(existing.file_path, existing.file_format)
                if existing.file_path and Path(existing.file_path).exists()
                else 0
            )
            new_rich = _richness(str(src), result.file_format)
            if cur_rich == 0 or new_rich > cur_rich:
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
        title=result.title or dest.stem,
        author=result.author,
        site=result.site,
        source_url=result.source_url,
        chapters_count=result.num_chapters,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    _apply_file(work, dest, result, sha1)
    session.add(work)
    session.commit()
    session.refresh(work)
    return work
