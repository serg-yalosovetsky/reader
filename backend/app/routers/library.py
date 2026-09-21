"""Роутер библиотеки: список произведений, карточка, загрузка файла вручную."""

from __future__ import annotations

import anyio
# Явно, а не полагаясь на ленивый __getattr__ пакета: он есть не во всех
# версиях anyio, а промах вылезет только в рантайме при загрузке книги.
import anyio.to_thread
import tempfile
import zipfile
from functools import partial
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

import os

from .. import covers
from ..db.models import Monitored, Progress, Work, utcnow
from ..db.session import get_session
from ..upload_identity import extract_identity
from ..storage import (
    detect_format,
    extract_books_from_zip,
    import_file,
    sha1_of_file,
)

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
                # Переносим на выжившего то, чего у него нет: calibre_id (иначе
                # следующий calibre-синк пересоздаст удалённый дубль — рецидив),
                # описание и обложку.
                if dup.calibre_id is not None and keep.calibre_id is None:
                    keep.calibre_id = dup.calibre_id
                if dup.description and not keep.description:
                    keep.description = dup.description
                if dup.cover_path and not keep.cover_path:
                    keep.cover_path = dup.cover_path
                    keep.cover_source = dup.cover_source
                session.add(keep)
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


# Поля, которые реально рисует список библиотеки (frontend/js/library.js).
# Всё остальное страница книги дотягивает через GET /api/library/{id}.
_LIST_COLUMNS = (
    Work.id,
    Work.title,
    Work.author,
    Work.calibre_id,
    Work.chapters_count,
    Work.series,
    Work.series_index,
    Work.updated_at,
    Work.content_updated_at,  # «обновлено» для карточки — про главы, не про чтение
    Work.cover_path,  # только ради cover_v ниже, наружу не отдаётся
)


@router.get("")
def list_works(
    hidden: bool = False, session: Session = Depends(get_session)
) -> list[dict]:
    """Все произведения, новые сверху — узким набором полей.

    Раньше здесь материализовался весь Work: 27 колонок × ~1400 записей, потом из
    словаря выбрасывалось description — то есть длинные аннотации ехали из БД
    только чтобы быть выкинутыми в Python. Сам Postgres в этом не виноват (6 мс
    из ~104), горит сериализация: ORM-материализация ~45%, json.dumps ~16%,
    model_dump ~13%.
    """
    result = []
    # Скрытые книги (serg/tasks#923) в обычном списке не показываются; фронт
    # берёт их отдельным запросом ?hidden=1 и только когда человек ищет.
    # `is_(True)` / `~is_(True)`, а не `== False`: у книг, заведённых до
    # появления колонки, значение может остаться NULL, и сравнение с False
    # потеряло бы их из библиотеки молча.
    cond = Work.hidden.is_(True) if hidden else ~Work.hidden.is_(True)
    rows = session.exec(
        select(*_LIST_COLUMNS).where(cond).order_by(Work.updated_at.desc())
    ).all()
    for r in rows:
        cover_path = r.cover_path
        cover_v = 0
        if cover_path:
            p = Path(cover_path)
            cover_v = int(p.stat().st_mtime) if p.exists() else 0
        result.append(
            {
                "id": r.id,
                "title": r.title,
                "author": r.author,
                "calibre_id": r.calibre_id,
                "chapters_count": r.chapters_count,
                "series": r.series,
                "series_index": r.series_index,
                "updated_at": r.updated_at,
                "content_updated_at": r.content_updated_at,
                "cover_v": cover_v,
            }
        )
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
            if w.cover_source == "manual":  # выбор человека (serg/tasks#1055)
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
def get_work(work_id: int, session: Session = Depends(get_session)) -> dict:
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    d = work.model_dump()
    d.update(_completeness(work, session))
    return d


def _completeness(work: Work, session: Session) -> dict:
    """Полнота книги для страницы «информация»: сколько глав у нас и сколько на
    сайте. Без этих двух чисел «недокачано» невозможно ни увидеть, ни проверить
    — книга просто молча стоит на месте (живой случай: 21 глава из 25).

    Считается только для одной книги (страница), а не для списка: обход файла
    на каждую карточку библиотеки был бы неоправданно дорогим.
    """
    from ...accounts.monitor import _metric_kind
    from ..services import count_sections

    # Нет файла — нет и глав «у нас». Иначе книга-ссылка (только в Calibre или
    # ещё не скачанная) показывала бы унаследованное от метаданных число как
    # будто она уже лежит на диске.
    have = (work.chapters_count or 0) if work.file_path else 0
    if not have and work.file_path:
        # Ленивый бэкфилл: поле появилось позже части книг, и без досчёта
        # страница показывала бы «— из 25» на давно скачанной книге.
        try:
            have = count_sections(
                work.file_path, work.file_format, book_title=work.title or ""
            )
        except Exception:  # noqa: BLE001
            have = 0
        if have and have != (work.chapters_count or 0):
            # Пересчёт по файлу разошёлся с сохранённым — значит контент менялся
            # мимо обычного пути (ручная замена файла, сбой при докачке).
            work.chapters_count = have
            work.content_updated_at = utcnow()
            session.add(work)
            session.commit()

    mons = session.exec(select(Monitored).where(Monitored.work_id == work.id)).all()
    # Из нескольких подписок берём ту, что видела больше глав — она и есть
    # самый полный известный источник.
    best = max(mons, key=lambda m: m.last_seen_chapters or 0, default=None)
    site = (best.last_seen_chapters or 0) if best else 0
    unit = (
        _metric_kind(best.last_seen_source or best.source_url) if best else "chapters"
    )
    return {
        "chapters_have": have,
        "chapters_site": site,
        # «страницы» у readli — величина ДРУГОГО рода, и подписывать её главами
        # нельзя: 21 глава «из 84» выглядела бы как потеря двух третей книги.
        "chapters_unit": unit,
        "monitored": bool(mons),
        "update_error": (best.last_error or "") if best else "",
        "update_paused": bool(best and (best.fail_count or 0) >= 5),
    }


def _add_work(
    session: Session, title: str, fmt: str, dest: Path, sha1: str, author: str = ""
) -> Work:
    """Завести запись о загруженной книге."""
    work = Work(
        title=title,
        author=author,
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


async def _import_one(session: Session, path: Path, title: str) -> tuple[Work, bool]:
    """Импортировать один файл книги. Возвращает (книга, новая ли она).

    Файловые операции — в поток: это async-обработчик, а sha1_of_file читает
    книгу целиком и import_file её копирует.
    """
    fmt = detect_format(path.name)
    if not fmt:
        raise HTTPException(400, f"неподдерживаемый формат: {path.name}")
    sha1 = await anyio.to_thread.run_sync(sha1_of_file, path)
    # Дедуп: если книга с таким SHA-1 уже есть — вернуть её.
    existing = session.exec(select(Work).where(Work.sha1 == sha1)).first()
    if existing:
        return existing, False
    dest, _ = await anyio.to_thread.run_sync(import_file, path, sha1)
    # Название и автор — из самого файла (serg/tasks#1055); имя файла — последний
    # запасной вариант. Разбор читает файл → в поток, а не в event loop; сбой не роняет загрузку.
    found_title, found_author = await anyio.to_thread.run_sync(
        extract_identity, path, fmt, title
    )
    return (
        _add_work(
            session, found_title or title or "Без названия", fmt, dest, sha1, found_author
        ),
        True,
    )


async def _import_zip(session: Session, zip_path: Path) -> dict:
    """Импортировать книги из zip: один файл внутри — одна книга, много — много."""
    with tempfile.TemporaryDirectory() as td:
        try:
            paths = await anyio.to_thread.run_sync(
                extract_books_from_zip, zip_path, Path(td)
            )
        except zipfile.BadZipFile:
            raise HTTPException(400, "файл не открывается как zip-архив") from None
        except ValueError as e:
            # Единственный ValueError отсюда — превышен лимит распаковки.
            raise HTTPException(400, str(e)) from None
        if not paths:
            raise HTTPException(400, "в архиве нет книг (.epub, .fb2, .pdf)")
        added: list[Work] = []
        duplicates: list[Work] = []
        for p in paths:
            work, is_new = await _import_one(session, p, p.stem)
            (added if is_new else duplicates).append(work)
    return {
        "ok": True,
        "added": len(added),
        "duplicates": len(duplicates),
        "works": [{"id": w.id, "title": w.title, "new": True} for w in added]
        + [{"id": w.id, "title": w.title, "new": False} for w in duplicates],
    }


@router.post("/upload", response_model=None)
async def upload_book(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> Work | dict:
    """Ручная загрузка EPUB/FB2/PDF либо zip-архива с книгами внутри."""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    is_zip = suffix == ".zip"
    if not detect_format(filename) and not is_zip:
        raise HTTPException(
            400, "поддерживаются только .epub, .fb2, .pdf и .zip с книгами внутри"
        )

    # Сохраняем во временный файл: и sha1, и распаковка работают по файлу.
    fd, tmp_name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_name)
    async with await anyio.open_file(tmp_path, "wb") as tmp:
        while chunk := await file.read(1 << 20):
            await tmp.write(chunk)
    try:
        if is_zip:
            return await _import_zip(session, tmp_path)
        work, _ = await _import_one(session, tmp_path, Path(filename).stem)
        return work
    finally:
        await anyio.to_thread.run_sync(partial(tmp_path.unlink, missing_ok=True))


class HiddenIn(BaseModel):
    hidden: bool


@router.put("/{work_id}/hidden")
def set_hidden(
    work_id: int, payload: HiddenIn, session: Session = Depends(get_session)
) -> dict:
    """Скрыть книгу из библиотеки или вернуть её обратно (serg/tasks#923).

    updated_at намеренно НЕ трогаем: он задаёт порядок библиотеки, и книга,
    которую вернули из скрытых, прыгала бы на первое место как свежая.
    """
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")
    work.hidden = bool(payload.hidden)
    session.add(work)
    session.commit()
    return {"ok": True, "id": work_id, "hidden": work.hidden}


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
