"""Роутер чтения: отдаёт файл книги для рендера во foliate-js на клиенте."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from pathlib import Path

from fastapi import Request, APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlmodel import Session

from .. import config, covers, imagegen
from ..db.models import Work
from ..db.session import get_session

router = APIRouter(prefix="/api/reader", tags=["reader"])

log = logging.getLogger("reader.cover")

_MEDIA = {
    "epub": "application/epub+zip",
    "fb2": "application/x-fictionbook+xml",
    "pdf": "application/pdf",
}

# Дедуп одновременной генерации одной и той же обложки (несколько <img> одной
# книги на странице / параллельные вкладки не должны плодить генерации).
_gen_locks: dict[int, threading.Lock] = {}
_gen_registry_lock = threading.Lock()

# ВАЖНО: генерация НЕ должна блокировать отдачу обложек. Ленивую генерацию
# уводим в фоновый пул (макс 2 разом), чтобы медленный Pollinations (1-120с) не
# забивал потоки uvicorn — иначе даже книги с готовой обложкой висят. Запрос
# /cover отдаёт готовое мгновенно либо 404 (фолбэк-текст), а обложка появляется
# на следующем заходе.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="covergen"
)
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _lock_for(work_id: int) -> threading.Lock:
    with _gen_registry_lock:
        lk = _gen_locks.get(work_id)
        if lk is None:
            lk = _gen_locks[work_id] = threading.Lock()
        return lk


def _bg_generate(work_id: int) -> None:
    """Фоновая ленивая генерация (в пуле): своя сессия, снимает in-flight флаг."""
    try:
        from ..db.session import engine

        with Session(engine) as session:
            _generate_and_persist(work_id, session, force=False)
    except Exception as e:  # noqa: BLE001
        log.warning("Фоновая генерация обложки work=%s упала: %s", work_id, e)
    finally:
        with _inflight_lock:
            _inflight.discard(work_id)


def _schedule_generation(work: Work) -> None:
    """Поставить ленивую генерацию в фоновую очередь (с дедупом)."""
    if not (
        config.IMAGE_GEN_ENABLED and work.title and work.cover_source != "gen_failed"
    ):
        return
    with _inflight_lock:
        if work.id in _inflight:
            return
        _inflight.add(work.id)
    _EXECUTOR.submit(_bg_generate, work.id)


def _schedule_generation_by_id(work_id: int) -> None:
    """Как _schedule_generation, но по id — короткой сессией (коннект БД не
    держится дольше чтения флагов)."""
    from ..db.session import engine

    with Session(engine) as s:
        work = s.get(Work, work_id)
        if work:
            _schedule_generation(work)


def _usable_cover(work: Work) -> Path | None:
    """Сохранённая обложка, если она вообще похожа на обложку.

    Мусор (баннер/логотип/плейсхолдер сайта) отбраковываем: иначе он навсегда
    занимает место настоящей обложки — ветки «взять из Calibre» и «перекачать
    с источника» ниже просто не выполняются (был живой случай: PNG 122x41).
    """
    if work and work.cover_path:
        p = Path(work.cover_path)
        if p.exists():
            try:
                if covers.is_generic_cover(p.read_bytes(), check_aspect=True):
                    return None
            except OSError:
                return None
            return p
    return None


def _try_real_cover(work: Work) -> tuple[Path | None, str]:
    """Найти НАСТОЯЩУЮ обложку книги (встроенную в файл или с сайта-источника/
    зеркал) — прежде чем прибегать к ИИ-генерации. Возвращает (путь, источник).

    Предпочитаем оригинал сгенерированному: при импорте источник мог быть под
    защитой/таймаутом, и книга ушла в ИИ. Здесь пробуем ещё раз, в т.ч. зеркала
    на других сайтах (у книги обложка бывает только на одном из них)."""
    if work.file_path and Path(work.file_path).exists():
        c = covers.extract_cover(work.file_path, work.file_format, work.sha1)
        if c:
            return c, "embedded"
    if work.source_url:
        c = covers.fetch_source_cover(
            work.source_url, work.sha1, work.title, work.author
        )
        if c:
            return c, "source"
    # PDF-книга (обычно из Calibre) без сохранённой обложки: рендерим первую
    # страницу — у технических книг (Packt/O'Reilly) это и есть обложка.
    if work.file_format == "pdf":
        pdf = work.file_path
        if not (pdf and Path(pdf).exists()):
            try:
                from ...calibre import sync as _csync

                pdf = _csync.ensure_cached(work.id)
            except Exception:  # noqa: BLE001
                pdf = None
        if pdf and Path(pdf).exists():
            sha = work.sha1 or (
                f"cal{work.calibre_id}" if work.calibre_id else f"w{work.id}"
            )
            c = covers.extract_pdf_cover(pdf, sha)
            if c:
                return c, "pdf"
    return None, ""


def _meta_of(work: Work) -> dict:
    return {
        "title": work.title,
        "author": work.author,
        "genres": work.genres,
        "description": work.description,
        "fandom": work.fandom,
        "rating": work.rating,
        "cover_brief": work.cover_brief,
    }


def _ensure_brief(work: Work) -> None:
    """Заполнить work.cover_brief через Ollama, если ещё пусто (кеш)."""
    if config.BRIEF_ENABLED and not (work.cover_brief or "").strip():
        brief = imagegen.summarize(_meta_of(work))
        if brief:
            work.cover_brief = brief


def _generate_and_persist(
    work_id: int, session: Session, *, force: bool, provider: str | None = None
) -> Path | None:
    """Сгенерировать обложку ИИ под локом и записать результат в БД."""
    lk = _lock_for(work_id)
    with lk:
        work = session.get(Work, work_id)
        if not work:
            return None
        existing = _usable_cover(work)  # другой поток мог успеть, пока ждали лок
        if existing and not force:
            return existing
        # Оригинал важнее генерации: перед ИИ ещё раз пробуем настоящую обложку
        # (встроенную + зеркала на других сайтах). ИИ — только если её нигде нет.
        if not force:
            real, real_src = _try_real_cover(work)
            if real:
                work.cover_path = str(real)
                work.cover_source = real_src
                session.add(work)
                session.commit()
                return real
        _ensure_brief(work)  # арт-бриф (Ollama) → в промпт; кешируется в work
        salt = (
            f"-{int(threading.current_thread().ident or 0) & 0xFFFF}" if force else ""
        )
        try:
            path = covers.generate_cover(
                _meta_of(work), work.sha1, salt=salt, provider=provider
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Генерация обложки work=%s упала: %s", work_id, e)
            path = None
        if path:
            work.cover_path = str(path)
            work.cover_source = "generated"
        else:
            work.cover_source = "gen_failed"
        session.add(work)
        session.commit()
        return path


def _file_etag(path: Path) -> str:
    """ETag ровно по формуле Starlette (md5 от "mtime-size").

    Свой вариант вычисления был бы хуже совместимого: ответ 200 объявляет ETag,
    посчитанный Starlette, и если 304 отдавать по другому правилу, браузер начнёт
    получать противоречивые валидаторы на один и тот же файл.
    """
    import hashlib

    st = path.stat()
    base = f"{st.st_mtime}-{st.st_size}"
    return f'"{hashlib.md5(base.encode(), usedforsecurity=False).hexdigest()}"'


def _not_modified(request, path: Path, fmt: str) -> Response | None:
    """304 без тела, если у клиента уже лежит эта же версия файла.

    Книга — самый тяжёлый ответ сервиса (до 39 МБ), и при открытии она ехала
    заново каждый раз. Здесь же решается свежесть: дорос фанфик новыми главами —
    поменялись mtime/size, ETag другой, совпадения нет, приедет новая версия.
    """
    inm = request.headers.get("if-none-match")
    if not inm:
        return None
    etag = _file_etag(path)
    if etag not in {t.strip() for t in inm.split(",")}:
        return None
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": "no-cache", "X-Book-Format": fmt or ""},
    )


def _serve(path: Path, sha1: str) -> FileResponse:
    mtime = int(path.stat().st_mtime)
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["ETag"] = f'"{sha1}-{mtime}"'
    return resp


# Превью обложек. Оригиналы весят в среднем 169 КБ (максимум 3.5 МБ, 1920×2880),
# а рисуются в ячейку ~160×240 px — на библиотеку это давало 95.7% веса страницы
# и мобильный LCP 7.1 с. Отдаём уменьшенный WEBP, оригинал остаётся нетронутым.
_THUMB_WIDTHS = (320, 640)  # 1x/2x для мобильной и десктопной сетки
_THUMB_DIR_NAME = "_thumbs"


def _thumb(src: Path, width: int) -> Path | None:
    """Кэшированное превью ширины `width` или None — тогда отдаётся оригинал.

    Ширина берётся только из белого списка: иначе произвольный ?w= позволил бы
    забить диск бесконечными вариантами одной картинки. Имя кэша включает mtime
    оригинала, поэтому обновлённая обложка сама получает новый файл, а старые
    варианты того же размера подчищаются.
    """
    if width not in _THUMB_WIDTHS:
        return None
    try:
        mtime = int(src.stat().st_mtime)
        out_dir = src.parent / _THUMB_DIR_NAME
        out = out_dir / f"{src.stem}_{width}_{mtime}.webp"
        if out.exists():
            return out

        from PIL import Image

        with Image.open(src) as im:
            if im.width <= width:
                return None  # оригинал и так не больше запрошенного
            height = round(im.height * width / im.width)
            im = im.convert("RGB").resize((width, height), Image.LANCZOS)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            im.save(tmp, "WEBP", quality=82, method=4)
            tmp.replace(out)  # атомарно: параллельный запрос не увидит огрызок

        for stale in out_dir.glob(f"{src.stem}_{width}_*.webp"):
            if stale != out:
                stale.unlink(missing_ok=True)
        return out
    except Exception:  # noqa: BLE001 — превью не должно ронять отдачу обложки
        return None


@router.get("/{work_id}/file")
def get_book_file(
    request: Request, work_id: int, original: bool = False
) -> Response:
    """Бинарь книги (EPUB/FB2). foliate-js грузит и рендерит его на клиенте.

    У PDF есть EPUB-версия (convert.py) — отдаём её: перетекающий текст вместо
    картинок-страниц. original=1 — принудительно оригинал (скачивание, сверка
    с вёрсткой). Фактический формат — в заголовке X-Book-Format (фронт по нему
    именует файл для foliate-js, иначе EPUB уедет в pdf.js).

    Коннект БД держится только на чтение метаданных; сама докачка из Calibre
    (до 180с) идёт БЕЗ занятого коннекта — иначе параллельные открытия забивали
    пул и книга «висла, срабатывала со второй попытки»."""
    from ..db.session import engine

    with Session(engine) as session:
        work = session.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        is_calibre_link = work.site == "calibre" and not work.file_path
        file_path = work.file_path
        file_format = work.file_format
        converted = work.converted_path if work.converted_status == "ready" else ""

    # EPUB-версия готова — отдаём её (оригинал остаётся доступен по original=1).
    if converted and not original:
        cpath = Path(converted)
        if cpath.exists():
            fresh = _not_modified(request, cpath, "epub")
            if fresh is not None:
                return fresh
            resp = FileResponse(
                cpath, media_type=_MEDIA["epub"], filename=cpath.name
            )
            # См. комментарий ниже: no-cache = «храни, но валидируй», а не «не храни».
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["X-Book-Format"] = "epub"
            return resp

    if is_calibre_link:
        # Книга-ссылка: тянем файл из Calibre по требованию в вытесняемый кэш.
        from ...calibre import sync as csync

        path = csync.ensure_cached(work_id)
        if not path:
            raise HTTPException(502, "не удалось получить файл из Calibre")
    else:
        if not file_path:
            raise HTTPException(404, "файл книги не найден")
        path = Path(file_path)
        if not path.exists():
            raise HTTPException(410, "файл книги отсутствует на диске")
    media = _MEDIA.get(file_format, "application/octet-stream")
    fresh = _not_modified(request, path, file_format or "")
    if fresh is not None:
        return fresh
    resp = FileResponse(path, media_type=media, filename=path.name)
    # Раньше здесь стоял no-store, и книга ехала по сети при КАЖДОМ открытии —
    # на замерах это давало 86% времени до появления текста. no-cache позволяет
    # браузеру хранить файл, но обязывает проверить его перед использованием:
    # FileResponse отдаёт ETag/Last-Modified, значит повторное открытие стоит
    # одного условного запроса и 304 без тела. Книга дорастает новыми главами —
    # меняется файл, меняется ETag, устаревшая версия не отдаётся.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Book-Format"] = file_format or ""
    return resp


@router.get("/{work_id}/cover")
@router.head("/{work_id}/cover")
def get_cover(work_id: int, w: int = 0) -> FileResponse:
    """Обложка книги. Если её нет — пробуем сгенерировать ИИ (лениво, 1 раз).
    Иначе 404 → фронт рисует текстовую заглушку.

    Коннект БД держится только на чтение метаданных; сетевой запрос обложки из
    Calibre идёт без него (см. get_book_file)."""
    from ..db.session import engine

    with Session(engine) as session:
        work = session.get(Work, work_id)
        if not work:
            raise HTTPException(404, "книги нет")
        path = _usable_cover(work)
        if path:
            return _serve(_thumb(path, w) or path, work.sha1)
        is_calibre = work.site == "calibre" and bool(work.calibre_id)
        sha_fallback = work.sha1 or (f"cal{work.calibre_id}" if work.calibre_id else "")

    # Книга Calibre: берём настоящую обложку из Calibre по требованию (кешируем).
    if is_calibre:
        from ...calibre import sync as csync

        cpath = csync.ensure_cover(work_id)
        if cpath:
            return _serve(cpath, sha_fallback)

    # Обложки нет — НЕ блокируем запрос генерацией: ставим её в фон (дедуп,
    # gen_failed не повторяем) и сразу отдаём 404 → фронт рисует текст-заглушку.
    # Обложка появится при следующем открытии библиотеки.
    _schedule_generation_by_id(work_id)
    raise HTTPException(404, "обложки нет")


@router.post("/{work_id}/cover/generate")
def regenerate_cover(
    work_id: int,
    force: bool = True,
    provider: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Принудительно сгенерировать/переснять обложку ИИ (кнопка на странице
    книги). force=1 игнорит существующую/gen_failed и рисует заново. provider=comfy
    — явно попросить FLUX (если ComfyUI доступен)."""
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "книги нет")
    path = _generate_and_persist(work_id, session, force=force, provider=provider)
    if not path:
        raise HTTPException(502, "не удалось сгенерировать обложку")
    return {
        "ok": True,
        "cover_v": int(path.stat().st_mtime),
        "cover_source": "generated",
    }
