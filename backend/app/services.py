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
from .db.models import Progress, Work, utcnow
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
    from .book_identity import extract_text_sample, same_book, work_descriptor

    ex = result.extra or {}
    rd = {
        "title": result.title or "",
        "author": result.author or "",
        "series": ex.get("series", ""),
        "series_index": ex.get("series_index", 0),
        "annotation": ex.get("annotation", ""),
    }
    _rt: dict = {}

    def _res_text() -> str:  # текст скачанного файла, считаем один раз
        if "t" not in _rt:
            _rt["t"] = extract_text_sample(result.file_path, result.file_format)
        return _rt["t"]

    if _norm(result.title):
        for w in session.exec(select(Work)).all():
            gtb = (
                (lambda w=w: extract_text_sample(w.file_path, w.file_format))
                if w.file_path
                else None
            )
            if same_book(rd, work_descriptor(w), get_text_a=_res_text, get_text_b=gtb):
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


def _unfinish_progress(session: Session, work_id: int, old_len: int, new_len: int) -> None:
    """Дозагрузились новые главы (текст книги вырос old_len→new_len) — прочитанная
    книга больше не «дочитана». Масштабируем ratio по длине; если он всё равно
    остаётся выше порога «прочитано» (0.98) из-за малого прироста — опускаем чуть
    ниже, чтобы фронт показал плашку «обновление» и не помечал книгу прочитанной.
    Реальная позиция чтения хранится в locator/text_anchor и не теряется."""
    if not new_len or new_len <= old_len:
        return
    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    if not prog or prog.ratio <= 0:
        return
    scaled = prog.ratio * (old_len / new_len)
    if scaled >= 0.98:
        scaled = 0.97
    if scaled < prog.ratio:
        prog.ratio = scaled
        session.add(prog)


_GENERIC_SECTION = re.compile(
    r"^(часть|part|страница|page|раздел|section)\s*[\dIVXLCM]+$", re.I
)


def _real_chapters(path, fmt) -> int:
    """Число секций с ОСМЫСЛЕННЫМ заголовком главы (не плейсхолдер «Часть N»).
    Отличает книгу с реальной разбивкой (author.today/новый readli — «Глава N»,
    именованные главы) от page-blob («Часть 1..N») или цельного файла. Заголовок
    книги в TOC тоже считается, но это одинаковая добавка у обоих сравниваемых —
    на относительное сравнение не влияет."""
    import zipfile

    try:
        p = str(path)
        low = (fmt or "").lower()
        titles: list[str] = []
        if low == "epub" or p.lower().endswith(".epub"):
            z = zipfile.ZipFile(p)
            ncx = [n for n in z.namelist() if n.lower().endswith(".ncx")]
            if ncx:
                titles = re.findall(
                    r"<text>(.*?)</text>", z.read(ncx[0]).decode("utf-8", "ignore"), re.S
                )
        elif low == "fb2" or p.lower().endswith(".fb2"):
            with open(p, encoding="utf-8", errors="ignore") as fh:
                data = fh.read()
            titles = re.findall(r"<section[^>]*>\s*<title>(.*?)</title>", data, re.S)
        real = 0
        for raw in titles:
            ttl = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            if ttl and not _GENERIC_SECTION.match(ttl):
                real += 1
        return real
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
            # Структурная оценка: сколько реальных глав (не «Часть N»-плейсхолдеров).
            cur_ch = (
                _real_chapters(existing.file_path, existing.file_format)
                if existing.file_path and Path(existing.file_path).exists()
                else 0
            )
            new_ch = _real_chapters(str(src), result.file_format)
            # Заменяем если: (а) новый полнее по тексту, ИЛИ (б) у нового ЕСТЬ реальная
            # разбивка на главы, а у текущего заметно меньше, при почти равном объёме
            # (не короче 90%) — структурный апгрейд page-blob → реальные главы.
            fuller = cur_rich == 0 or new_rich > cur_rich
            better_structure = new_ch > cur_ch and new_rich >= cur_rich * 0.9
            if fuller or better_structure:
                dest, _ = import_file(src, sha1)
                _apply_file(existing, dest, result, sha1)
                if cur_rich and new_rich > cur_rich:
                    _unfinish_progress(session, existing.id, cur_rich, new_rich)
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
