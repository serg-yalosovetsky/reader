"""Синхронизация каталога Calibre в таблицу Work — ссылки, без копий файлов.

Каждая книга Calibre = Work(site="calibre", calibre_id=..., file_path=""). Файл
НЕ копируется в ридер: тянется по требованию при открытии (fetch-on-open в
routers/reader.py) в evictable-кэш. Так книга физически живёт только в Calibre,
а ридер держит ссылку + свой прогресс/закладки (по work_id). Нет дублирования.

migrate_local_to_refs() — одноразовый перевод уже загруженных локальных копий на
ссылки: матч по нормализованным (title, author) против каталога Calibre, флип
полей Work НА МЕСТЕ (progress/bookmarks сохраняются автоматически — они по
work_id), удаление локального файла. Фанфики (ficbook/authortoday), которых нет
в Calibre, остаются локальными.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

from pathlib import Path

from sqlmodel import Session, select

from ..app.config import CALIBRE_CACHE_DIR, CALIBRE_CACHE_MAX_MB, COVERS_DIR
from ..app.db.models import Blacklist, Bookmark, Highlight, Monitored, Progress, Work, utcnow
from . import client

log = logging.getLogger("reader.calibre.sync")

# «Книжные» источники — для них разрешён fallback-матч только по названию.
# Фанфик-источники исключены, чтобы случайный однотитульник не увёл фик в Calibre.
_BOOK_SITES = {"upload", "calibre", "readli", "searchfloor", ""}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"“”«»'`]", "", (s or "")).strip().lower())


def _norm_title(s: str) -> str:
    """Нормализация названия для матчинга: срезаем ведущую нумерацию тома
    («04 - », «07.», «02) ») и хвостовую пунктуацию — частый мусор имён файлов."""
    t = _norm(s)
    t = re.sub(r"^\d{1,3}\s*[-.)]\s*", "", t)
    return t.rstrip(". ")


# ----------------------- каталог Calibre в память -----------------------


def _catalog_maps():
    """(by_ta, by_t): книги Calibre по (norm title, norm author) и по norm title."""
    by_ta: dict[tuple, list[dict]] = {}
    by_t: dict[str, list[dict]] = {}
    for b in client.list_books():
        nt, na = _norm_title(b["title"]), _norm(b.get("authors", ""))
        by_ta.setdefault((nt, na), []).append(b)
        by_t.setdefault(nt, []).append(b)
    return by_ta, by_t


def _match(work: Work, by_ta, by_t):
    """Найти книгу Calibre для локального Work или None."""
    nt, na = _norm_title(work.title), _norm(work.author)
    # 1) строгий матч title+author
    got = by_ta.get((nt, na))
    if got and len(got) == 1:
        return got[0]
    # частичное совпадение автора (calibre — один автор, локально может быть «A & B»)
    if got:
        return got[0]
    # 2) fallback по одному названию — только для «книжных» источников и если титул уникален
    if work.site in _BOOK_SITES:
        gt = by_t.get(nt)
        if gt and len(gt) == 1:
            return gt[0]
    return None


# ----------------------- merge при коллизии -----------------------


def merge_work(session: Session, keep: Work, drop: Work) -> None:
    """Слить drop в keep: перенести закладки/выделения/мониторинг, оставить лучший
    Progress (Progress уникален по work_id), удалить drop и его файл."""
    for bm in session.exec(select(Bookmark).where(Bookmark.work_id == drop.id)).all():
        bm.work_id = keep.id
        session.add(bm)
    for hl in session.exec(select(Highlight).where(Highlight.work_id == drop.id)).all():
        hl.work_id = keep.id
        session.add(hl)
    for m in session.exec(select(Monitored).where(Monitored.work_id == drop.id)).all():
        m.work_id = keep.id
        session.add(m)
    kp = session.exec(select(Progress).where(Progress.work_id == keep.id)).first()
    dp = session.exec(select(Progress).where(Progress.work_id == drop.id)).first()
    if dp:
        if kp is None:
            dp.work_id = keep.id
            session.add(dp)
        else:
            # оставить более продвинутый/свежий прогресс
            if (dp.ratio, dp.last_read_time) > (kp.ratio, kp.last_read_time):
                kp.ratio = dp.ratio
                kp.locator = dp.locator
                kp.last_read_time = dp.last_read_time
                kp.source = dp.source
                session.add(kp)
            session.delete(dp)
    old = drop.file_path
    session.delete(drop)
    if old and old != keep.file_path and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass


# ----------------------- миграция локальных копий -> ссылки -----------------------


def migrate_local_to_refs(session: Session, dry_run: bool = True) -> dict:
    if not client.http_mode():
        return {"status": "skipped", "reason": "not http mode"}
    by_ta, by_t = _catalog_maps()

    locals_ = [w for w in session.exec(select(Work)).all() if w.file_path]
    assigned: dict[
        int, Work
    ] = {}  # calibre_id -> уже назначенный ref (в этой миграции)
    # уже существующие валидные ref (без файла)
    for w in session.exec(select(Work).where(Work.site == "calibre")).all():
        if not w.file_path and w.calibre_id is not None:
            assigned[w.calibre_id] = w

    converted = merged = unmatched = 0
    samples_conv, samples_unm = [], []

    for w in locals_:
        b = _match(w, by_ta, by_t)
        if not b:
            unmatched += 1
            if len(samples_unm) < 8:
                samples_unm.append(f"{w.site}: {w.title[:40]} / {w.author[:20]}")
            continue
        cid = b["calibre_id"]
        old_file = w.file_path
        if cid in assigned and assigned[cid] is not w:
            # книга уже представлена ссылкой -> слить дубликат
            merged += 1
            if not dry_run:
                merge_work(session, assigned[cid], w)
            continue
        # конвертация на месте (progress/bookmarks сохраняются — они по work_id)
        converted += 1
        if len(samples_conv) < 8:
            samples_conv.append(f"{w.title[:38]} -> calibre#{cid}")
        if not dry_run:
            w.calibre_id = cid
            w.site = "calibre"
            # Формат берём из Calibre только когда своего файла нет: у локальной
            # копии формат уже известен и менять его на «лучший по каталогу»
            # значит соврать про файл, который лежит на диске.
            if not w.file_path:
                w.file_format = client.best_format(b["formats"]) or w.file_format
            if not w.description:
                w.description = b.get("description") or ""
            if not w.genres and b.get("tags"):
                w.genres = json.dumps(b["tags"], ensure_ascii=False)
            w.meta_synced = True
            session.add(w)
            assigned[cid] = w
            # Локальный файл НЕ удаляем. Раньше он стирался «ради экономии», после
            # чего книга открывалась только докачкой с Calibre — на замерах это
            # давало основную часть времени открытия, а при забитом кэше ещё и
            # повторялось каждый раз. Диск дешевле ожидания: вся локальная
            # библиотека занимала 700 МБ против 2 ГБ кэша временных копий.
        else:
            assigned[cid] = w

    if not dry_run:
        session.commit()
    return {
        "status": "ok",
        "dry_run": dry_run,
        "local_total": len(locals_),
        "converted": converted,
        "merged": merged,
        "unmatched": unmatched,
        "sample_converted": samples_conv,
        "sample_unmatched": samples_unm,
    }


# ----------------------- полный catalog-sync -----------------------


def sync_catalog(session: Session) -> dict:
    """Upsert Work-ссылки по всем книгам Calibre (site=calibre). Идемпотентно."""
    if not client.http_mode():
        return {"status": "skipped", "reason": "not http mode"}
    from ..app import book_identity as bi

    # calibre_id может «переехать» на не-calibre работу при дедупе — матчимся
    # по нему среди ВСЕХ работ, иначе синк пересоздавал бы удалённый дубль.
    existing: dict[int, Work] = {}
    by_title: dict[tuple, list[Work]] = {}
    for w in session.exec(select(Work)).all():
        if w.calibre_id is not None:
            existing[w.calibre_id] = w
        by_title.setdefault(bi._title_key(w.title), []).append(w)

    # Книги, удалённые пользователем «крестиком» (чёрный список по title+author),
    # не воскрешаем из каталога Calibre: книга физически остаётся в Calibre, и без
    # этой проверки плановый синк создавал её Work заново (source_url пуст → монитор
    # не при чём; drive_books/monitor блэклист уважают, а sync_catalog — нет). _norm
    # совпадает с services._norm, которым delete_work писал title_norm/author_norm.
    bl_pairs = {
        (bl.title_norm, bl.author_norm)
        for bl in session.exec(select(Blacklist)).all()
        if bl.title_norm
    }

    added = updated = skipped_blacklist = 0
    seen = set()
    for b in client.iter_opds_books():
        cid = b["calibre_id"]
        seen.add(cid)
        fmt = client.best_format(b["formats"])
        if not fmt:
            continue
        genres = json.dumps(b.get("tags") or [], ensure_ascii=False)
        desc = b.get("description") or ""
        w = existing.get(cid)
        if w is None:
            # Удалена пользователем — не воскрешать (ни стабом, ни привязкой calibre_id).
            if (_norm(b["title"]), _norm(b["authors"])) in bl_pairs:
                skipped_blacklist += 1
                continue
            # Книга уже есть из другого источника (upload/AT/...)? Привязываем
            # calibre_id к ней вместо создания стаба-дубля: то же нормализованное
            # название (с томом) и совместимый автор (пустой или совпадающий).
            cand = None
            cal_a = (b["authors"] or "").strip().lower()
            for ex in by_title.get(bi._title_key(b["title"]), []):
                if ex.calibre_id is not None:
                    continue
                ex_a = (ex.author or "").strip().lower()
                if not ex_a or not cal_a or ex_a == cal_a \
                        or ex_a in cal_a or cal_a in ex_a:
                    cand = ex
                    break
            if cand is not None:
                cand.calibre_id = cid
                if desc and not cand.description:
                    cand.description = desc
                if genres != "[]" and not cand.genres:
                    cand.genres = genres
                session.add(cand)
                existing[cid] = cand
                updated += 1
                continue
            session.add(
                Work(
                    title=b["title"],
                    author=b["authors"],
                    site="calibre",
                    calibre_id=cid,
                    file_path="",
                    file_format=fmt,
                    sha1="",
                    source_url="",
                    description=desc,
                    genres=genres,
                    meta_synced=True,
                    created_at=utcnow(),
                    # НЕ utcnow(): библиотека сортируется по updated_at, и книга,
                    # годами лежащая в каталоге Calibre, всплывала бы наверх в тот
                    # момент, когда синк впервые завёл на неё Work-ссылку. Берём
                    # дату из каталога; нет её — уводим вниз (created_at остаётся
                    # честным «когда запись появилась у нас»).
                    updated_at=b.get("updated") or datetime(1970, 1, 1),
                )
            )
            added += 1
        else:
            changed = False
            if not w.file_path and w.file_format != fmt:
                w.file_format = fmt
                changed = True
            if desc and not w.description:
                w.description = desc
                changed = True
            if genres != "[]" and not w.genres:
                w.genres = genres
                changed = True
            if changed:
                # Обогащение метаданных — не контент-событие: updated_at не
                # бампаем, иначе плановый синк ломает сортировку библиотеки.
                session.add(w)
                updated += 1
    session.commit()
    return {
        "status": "ok",
        "added": added,
        "updated": updated,
        "skipped_blacklist": skipped_blacklist,
        "catalog_total": len(seen),
    }


# ----------------------- fetch-on-open (evictable кэш) -----------------------


def drop_converted_sources() -> int:
    """Выбросить из кэша исходники, у которых готова EPUB-версия.

    PDF читается через сконвертированный EPUB (перетекающий текст), сам исходник
    после конвертации не нужен — а весит он десятки мегабайт и вытесняет из кэша
    книги, которые читают. Возвращает освобождённые байты.

    Безопасно: это КЭШ, оригинал остаётся в Calibre. Понадобится снова —
    скачается заново.
    """
    from ..app.db.session import engine

    if not CALIBRE_CACHE_DIR.exists():
        return 0
    freed = 0
    with Session(engine) as s:
        for path in list(CALIBRE_CACHE_DIR.iterdir()):
            if not path.is_file() or path.suffix.lower() != ".pdf":
                continue
            try:
                calibre_id = int(path.stem)
            except ValueError:
                continue
            work = s.exec(
                select(Work).where(Work.calibre_id == calibre_id)
            ).first()
            if not work or work.converted_status != "ready":
                continue
            conv = work.converted_path
            if not conv or not Path(conv).exists():
                continue   # EPUB обещан, но его нет — исходник ещё нужен
            try:
                size = path.stat().st_size
                path.unlink()
                freed += size
            except OSError:
                continue
    if freed:
        log.info("Кэш Calibre: освобождено %.1f МБ PDF-исходников", freed / 1048576)
    return freed


def _evict_cache() -> None:
    """LRU-подрезка кэша: если суммарный размер > лимита — удаляем самые старые
    (по mtime) файлы, пока не влезем. Прогресс/закладки не трогаются (они в БД).

    Перед вытеснением освобождаем место за счёт исходников, у которых уже есть
    EPUB-версия: иначе несколько PDF по 60 МБ выдавливают из кэша десятки книг,
    которые действительно читают.
    """
    cap = CALIBRE_CACHE_MAX_MB * 1024 * 1024
    if not CALIBRE_CACHE_DIR.exists():
        return
    try:
        drop_converted_sources()
    except Exception:  # noqa: BLE001 — освобождение места не должно ронять докачку
        log.warning("Не удалось прибрать PDF-исходники из кэша", exc_info=True)
    files = [
        p
        for p in CALIBRE_CACHE_DIR.iterdir()
        if p.is_file() and not p.name.endswith(".part")
    ]
    total = sum(p.stat().st_size for p in files)
    if total <= cap:
        return
    for p in sorted(files, key=lambda x: x.stat().st_mtime):
        try:
            total -= p.stat().st_size
            p.unlink()
        except OSError:
            continue
        if total <= cap:
            break


def ensure_cached(work_id: int) -> Path | None:
    """Гарантировать локальный файл calibre-книги в кэше (скачать при отсутствии).
    Возвращает путь или None. Кэш вытесняемый — книга остаётся в Calibre.

    ВАЖНО: коннект БД НЕ держится во время сетевой докачки (до 180с). Читаем
    метаданные короткой сессией, отпускаем коннект, качаем, затем короткой
    сессией дописываем sha1. Иначе параллельные открытия забивали пул коннектов
    Postgres и книга «висла».
    """
    from ..app.db.session import engine

    with Session(engine) as s:
        work = s.get(Work, work_id)
        if (
            not work
            or work.site != "calibre"
            or not work.calibre_id
            or not client.http_mode()
        ):
            return None
        calibre_id = work.calibre_id
        fmt = (work.file_format or "epub").lower()
        have_sha = bool(work.sha1)

    CALIBRE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CALIBRE_CACHE_DIR / f"{calibre_id}.{fmt}"
    if dest.exists() and dest.stat().st_size > 0:
        dest.touch()  # обновляем mtime для LRU
        return dest
    try:
        path, sha = client.download_book(calibre_id, fmt, dest)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось скачать calibre#%s: %s", calibre_id, e)
        return None
    if not have_sha:
        with Session(engine) as s:
            w = s.get(Work, work_id)
            if w and not w.sha1:
                w.sha1 = sha
                s.add(w)
                s.commit()
    _evict_cache()
    return path


def ensure_cover(work_id: int) -> Path | None:
    """Обложка calibre-книги: берём /opds/cover в COVERS_DIR, кешируем в cover_path.

    Коннект БД не держится во время сетевого запроса обложки (см. ensure_cached).
    """
    from ..app.db.session import engine

    with Session(engine) as s:
        work = s.get(Work, work_id)
        if not work:
            return None
        if work.cover_path and Path(work.cover_path).exists():
            # Кешированный файл принимаем, только если он похож на обложку:
            # сохранённый когда-то баннер/логотип иначе навсегда вытесняет
            # настоящую обложку из Calibre (живой случай: PNG 122x41).
            from ..app import covers as _covers

            try:
                if not _covers.is_generic_cover(
                    Path(work.cover_path).read_bytes(), check_aspect=True
                ):
                    return Path(work.cover_path)
            except OSError:
                pass
        if work.site != "calibre" or not work.calibre_id or not client.http_mode():
            return None
        calibre_id = work.calibre_id

    data = client.cover_bytes(calibre_id)
    if not data:
        return None
    from ..app import covers

    if covers.is_generic_cover(data):
        return None  # Calibre отдал дженерик-заглушку — не обложка
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    p = COVERS_DIR / f"calibre_{calibre_id}.jpg"
    p.write_bytes(data)
    with Session(engine) as s:
        w = s.get(Work, work_id)
        if w:
            w.cover_path = str(p)
            w.cover_source = "calibre"
            s.add(w)
            s.commit()
    return p
