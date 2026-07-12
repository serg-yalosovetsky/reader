"""Роутер библиотеки: список произведений, карточка, загрузка файла вручную."""

from __future__ import annotations

import anyio
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


def _dedup_works(session: Session) -> int:
    """Схлопнуть дубли книг (одинаковые название+автор): оставить самый полный
    файл, перевесить на него мониторинг, снести прогресс и файлы дублей.
    Возвращает число удалённых книг."""
    from collections import defaultdict

    from .. import book_identity as bi

    def _dsc(w: Work) -> dict:
        d = bi.work_descriptor(w)
        d["annotation"] = w.description or ""
        return d

    # Группируем по базовому названию (дёшево), внутри — кластеризуем по same_book:
    # одна книга с автором-ником и настоящим именем сливается, тёзки и разные тома —
    # нет (устойчиво к записи автора на разных ресурсах).
    buckets: dict[str, list[Work]] = defaultdict(list)
    for w in session.exec(select(Work)).all():
        buckets[bi._title_key(w.title)[0]].append(w)

    removed_works = 0
    for ws in buckets.values():
        if len(ws) <= 1:
            continue
        clusters: list[list[Work]] = []
        for w in ws:
            gta = (
                (lambda w=w: bi.extract_text_sample(w.file_path, w.file_format))
                if w.file_path
                else None
            )
            for cl in clusters:
                c0 = cl[0]
                gtb = (
                    (lambda c0=c0: bi.extract_text_sample(c0.file_path, c0.file_format))
                    if c0.file_path
                    else None
                )
                if bi.same_book(_dsc(w), _dsc(c0), get_text_a=gta, get_text_b=gtb):
                    cl.append(w)
                    break
            else:
                clusters.append([w])
        for cl in clusters:
            if len(cl) <= 1:
                continue
            # Безопасность: не сливаем сборники/омнибусы без автора (upload с пустым
            # автором) — иначе рискуем склеить их с одиночной книгой того же названия.
            if any(w.site == "upload" and not (w.author or "").strip() for w in cl):
                continue
            cl.sort(key=lambda w: _fsize(w.file_path), reverse=True)  # полный — первым
            keep = cl[0]
            for dup in cl[1:]:
                for m in session.exec(
                    select(Monitored).where(Monitored.work_id == dup.id)
                ).all():
                    m.work_id = keep.id
                    session.add(m)
                for p in session.exec(
                    select(Progress).where(Progress.work_id == dup.id)
                ).all():
                    session.delete(p)
                if dup.file_path and dup.file_path != keep.file_path:
                    try:
                        os.remove(dup.file_path)
                    except OSError:
                        pass
                session.delete(dup)
                removed_works += 1
    session.commit()
    return removed_works


def _backfill_covers(session: Session) -> int:
    """Дозаполнить настоящие обложки: встроенная в файл → с сайта-источника/зеркал.
    ИИ-обложку (generated/gen_failed) считаем временной и всегда вытесняем
    найденной настоящей. Возвращает число обновлённых книг."""
    added_covers = 0
    for w in session.exec(select(Work)).all():
        # ИИ-обложка (сгенерирована / генерация не удалась) — временная заглушка,
        # её ВСЕГДА вытесняем настоящей, если удалось найти.
        is_ai = w.cover_source in ("generated", "gen_failed")
        # Уже есть настоящая (не-ИИ) обложка — не трогаем.
        if w.cover_path and os.path.exists(w.cover_path) and not is_ai:
            continue
        c = None
        src = ""
        # 1) встроенная в файл книги (самый надёжный источник).
        if w.file_path and os.path.exists(w.file_path):
            c = covers.extract_cover(w.file_path, w.file_format, w.sha1)
            src = "embedded" if c else ""
        # 2) настоящая обложка с сайта-источника или зеркал на других сайтах.
        #    Даже поверх ИИ: реальная обложка приоритетнее сгенерированной. Раньше
        #    источник для ИИ-книг НЕ пробовался — и книга, у которой обложка не
        #    скачалась с первого раза (сайт под защитой/таймаут), НАВСЕГДА
        #    оставалась с ИИ-картинкой. Дженерик-баннер отсекается по md5+форме
        #    кадра внутри fetch_source_cover, так что хорошую ИИ он не затрёт.
        if not c and w.source_url:
            c = covers.fetch_source_cover(w.source_url, w.sha1, w.title, w.author)
            src = "source" if c else ""
        if c:
            w.cover_path = str(c)
            w.cover_source = src
            session.add(w)
            added_covers += 1
    session.commit()
    return added_covers


@router.post("/maintenance")
def maintenance(session: Session = Depends(get_session)) -> dict:
    """Убрать дубликаты книг (оставить самый полный файл), подчистить мониторинг,
    добэкафиллить обложки."""
    removed_works = _dedup_works(session)

    # Дедуп мониторинга: одна запись на work_id/source_url + снятие ложных
    # has_update (см. accounts.dedup — единый переиспользуемый модуль).
    from ...accounts.dedup import dedup_monitored

    removed_mon = dedup_monitored(session)["removed"]

    added_covers = _backfill_covers(session)
    return {
        "removed_duplicates": removed_works,
        "removed_monitored": removed_mon,
        "covers_added": added_covers,
    }


@router.get("")
def list_works(session: Session = Depends(get_session)) -> list[dict]:
    """Все произведения, новые сверху."""
    result = []
    for w in session.exec(select(Work).order_by(Work.updated_at.desc())).all():
        d = w.model_dump()
        # description в списке не нужен (может быть длинным ×308) — страница книги
        # дотягивает его через GET /api/library/{id}.
        d.pop("description", None)
        if w.cover_path:
            p = Path(w.cover_path)
            d["cover_v"] = int(p.stat().st_mtime) if p.exists() else 0
        else:
            d["cover_v"] = 0
        result.append(d)
    return result


@router.post("/backfill-meta")
def backfill_meta(
    session: Session = Depends(get_session), limit: int = 0, force: bool = False
) -> dict:
    """Разобрать метаданные (описание/жанры/статус/рейтинг) из локальных файлов
    (epub-opf и fb2) для книг, где они ещё не заполнены. Без сети."""
    from .. import bookmeta

    updated = 0
    scanned = 0
    q = select(Work) if force else select(Work).where(Work.meta_synced == False)  # noqa: E712
    for w in session.exec(q).all():
        if limit and updated >= limit:
            break
        if not w.file_path or not os.path.exists(w.file_path):
            continue
        scanned += 1
        meta = bookmeta.extract_meta(w.file_path, w.file_format)
        if not meta:
            continue
        if bookmeta.apply_meta(w, meta, overwrite=True):
            session.add(w)
            updated += 1
    session.commit()
    return {"scanned": scanned, "updated": updated}


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
        at_words = {w.lower().strip(".,") for w in at_author.split() if len(w) > 2}
        return bool(our_words & at_words)

    def _at_author(at_url: str) -> str:
        try:
            r = _httpx.get(
                at_url,
                timeout=10,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            _pat = "itemprop=['\"{0,1}author['\"{0,1}[^>]*>([^<]{2,60})<"
            m = _re.search(_pat, r.text)
            if not m:
                m = _re.search(
                    r"book-authors[^>]*>.*?href=[^>]+>([^<]{2,60})<", r.text, _re.S
                )
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


@router.post("/scan-drive-books")
def scan_drive_books(
    days: int = 7,
    limit: int = 30,
    commit: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """Сканировать gdrive:ReadEra/Books и импортировать недавно добавленные книги.
    dry-run по умолчанию (commit=false) — вернёт список кандидатов, ничего не меняя."""
    from ..drive_books import scan

    return scan(session, days=days, limit=limit, commit=commit)


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
    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_name)
    async with await anyio.open_file(tmp_path, "wb") as tmp:
        while chunk := await file.read(1 << 20):
            await tmp.write(chunk)
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


@router.delete("/{work_id}/update-flag")
def clear_update_flag(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Сбросить has_update для книги (пользователь дочитал до конца)."""
    mons = session.exec(select(Monitored).where(Monitored.work_id == work_id)).all()
    for m in mons:
        if m.has_update:
            m.has_update = False
            session.add(m)
    session.commit()
    return {"ok": True}


@router.delete("/{work_id}")
def delete_work(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Удалить книгу из библиотеки (файл + БД)."""
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    # Чёрный список: запоминаем книгу (название/автор + все source_url), чтобы
    # фиды и монитор её больше не докачивали и не показывали в библиотеке.
    from ..blacklist import add_entry as _bl_add

    _urls = [work.source_url] + [
        m.source_url
        for m in session.exec(
            select(Monitored).where(Monitored.work_id == work_id)
        ).all()
    ]
    _bl_add(session, title=work.title, author=work.author, urls=_urls)
    for p in session.exec(select(Progress).where(Progress.work_id == work_id)).all():
        session.delete(p)
    for m in session.exec(select(Monitored).where(Monitored.work_id == work_id)).all():
        session.delete(m)
    if work.file_path:
        try:
            os.remove(work.file_path)
        except OSError:
            pass
    if work.cover_path:
        try:
            os.remove(work.cover_path)
        except OSError:
            pass
    session.delete(work)
    session.commit()
    return {"ok": True}
