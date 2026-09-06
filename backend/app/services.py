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
    # Единственная точка, где у книги меняется содержимое, — и создание, и
    # докачка новых глав проходят здесь. Отсюда и берётся «обновлено» для
    # человека: updated_at для этого не годится, его двигает любое сохранение
    # прогресса чтения.
    work.content_updated_at = utcnow()
    # Число глав считаем ПО ФАЙЛУ, а не по тому, что сказал загрузчик: адаптеры
    # рапортуют по-разному (fb2 приходит одним куском с num_chapters=0), и в
    # результате поле оставалось от предыдущей, менее полной версии книги —
    # «21 глава» у файла, в котором их уже 25. count_sections — та же метрика,
    # которой монитор меряет полноту докачки (см. спеку update-pipeline).
    counted = 0
    try:
        counted = count_sections(dest, result.file_format, book_title=result.title or "")
    except Exception:  # noqa: BLE001 — битый файл не должен ронять регистрацию
        counted = 0
    if counted or result.num_chapters:
        work.chapters_count = counted or result.num_chapters
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
            data = fh.read()
        # fb2 <binary> — base64 обложки/картинок, НЕ текст книги. Без выреза
        # зеркало с жирной обложкой «перевешивает» зеркало с лишней главой.
        data = re.sub(r"<binary\b[^>]*>.*?</binary>", " ", data, flags=re.S)
        return len(re.sub(r"<[^>]+>", " ", data))
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


# Служебные записи TOC — не главы книги (FanFicFare кладёт «Title Page», сборщики —
# обложку и оглавление).
_SERVICE_SECTION = re.compile(
    r"^(title\s*page|cover|обложка|титул|титульный\s*лист|contents|"
    r"table\s*of\s*contents|оглавление|содержание)$",
    re.I,
)


def _section_titles(path, fmt) -> list[str]:
    """Заголовки секций книги: TOC (epub NCX) либо <section><title> (fb2)."""
    import zipfile

    try:
        p = str(path)
        low = (fmt or "").lower()
        raw_titles: list[str] = []
        if low == "epub" or p.lower().endswith(".epub"):
            z = zipfile.ZipFile(p)
            ncx = [n for n in z.namelist() if n.lower().endswith(".ncx")]
            if ncx:
                raw_titles = re.findall(
                    r"<text>(.*?)</text>", z.read(ncx[0]).decode("utf-8", "ignore"), re.S
                )
        elif low == "fb2" or p.lower().endswith(".fb2"):
            with open(p, encoding="utf-8", errors="ignore") as fh:
                data = fh.read()
            raw_titles = re.findall(r"<section[^>]*>\s*<title>(.*?)</title>", data, re.S)
        out: list[str] = []
        for raw in raw_titles:
            ttl = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            if ttl:
                out.append(ttl)
        return out
    except Exception:  # noqa: BLE001
        return []


def count_sections(path, fmt, *, book_title: str = "") -> int:
    """Сколько ГЛАВ реально лежит в файле — без суждений о качестве их названий.

    Отвечает на вопрос «всё ли докачано», в отличие от `_real_chapters`, который
    отвечает на «у какого из двух файлов разбивка лучше» и потому выбрасывает
    плейсхолдеры «Часть N». Для полноты такой фильтр — катастрофа: на ficbook главы
    ЧАСТО так и называются («Часть 1..14» — настоящее авторское название), и
    полностью скачанная книга выглядит как «2 главы из 14».

    Служебные записи TOC (Title Page / обложка / оглавление) и строка с названием
    книги не считаются: результат сравнивается с числом глав НА САЙТЕ, а не с другим
    файлом, поэтому лишняя добавка здесь не сокращается.
    """
    bt = re.sub(r"\s+", " ", (book_title or "")).strip().lower()
    n = 0
    for ttl in _section_titles(path, fmt):
        if _SERVICE_SECTION.match(ttl):
            continue
        if bt and ttl.lower() == bt:
            continue
        n += 1
    return n


def _real_chapters(path, fmt) -> int:
    """Число секций с ОСМЫСЛЕННЫМ заголовком главы (не плейсхолдер «Часть N»).
    Отличает книгу с реальной разбивкой (author.today/новый readli — «Глава N»,
    именованные главы) от page-blob («Часть 1..N») или цельного файла. Заголовок
    книги в TOC тоже считается, но это одинаковая добавка у обоих сравниваемых —
    на относительное сравнение не влияет.

    НЕ годится для проверки «всё ли докачано» — для этого count_sections().
    """
    real = 0
    for ttl in _section_titles(path, fmt):
        if not _GENERIC_SECTION.match(ttl):
            real += 1
    return real


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
            # Сколько ГЛАВ в каждом файле — без суждений о качестве их названий
            # (count_sections, а не _real_chapters: на ficbook «Часть N» — настоящие
            # авторские названия, см. spec.reader.update-pipeline v5). 0 = посчитать не
            # удалось (цельный fb2) — тогда этот сигнал просто не участвует.
            cur_sec = (
                count_sections(
                    existing.file_path,
                    existing.file_format,
                    book_title=existing.title or "",
                )
                if existing.file_path and Path(existing.file_path).exists()
                else 0
            )
            new_sec = count_sections(
                str(src), result.file_format, book_title=result.title or ""
            )
            # Заменяем если: (а) новый полнее по тексту, ИЛИ (б) у нового ЕСТЬ реальная
            # разбивка на главы, а у текущего заметно меньше, при почти равном объёме
            # (не короче 90%) — структурный апгрейд page-blob → реальные главы,
            # ИЛИ (в) в новом ПРОСТО БОЛЬШЕ ГЛАВ при сопоставимом объёме.
            #
            # (в) добавлено в v8. Без него растущая книга застревала навсегда, если
            # лежащая копия пришла с более «толстого» зеркала: объём текста может
            # УМЕНЬШАТЬСЯ при РОСТЕ числа глав. Живой случай: work 58 «Сломанный
            # Меч» — свежий ficbook давал 78 глав и richness 4 471 769, а старый файл —
            # 77 глав и 4 537 924; ни fuller, ни better_structure не срабатывали, и
            # 78-я глава не могла доехать ни при какой докачке.
            fuller = cur_rich == 0 or new_rich > cur_rich
            better_structure = new_ch > cur_ch and new_rich >= cur_rich * 0.9
            more_chapters = (
                bool(cur_sec)
                and bool(new_sec)
                and new_sec > cur_sec
                and new_rich >= cur_rich * 0.9
            )
            # Версионированный источник (документация Python): свежий файл
            # актуальнее по определению, спрашивать объём текста нельзя. Патч-
            # релиз может ВЫЧИСТИТЬ текст (удалённые модули, сокращённые
            # примеры) — тогда все три критерия ниже ложны, файл не заменяется,
            # а last_seen всё равно сдвигается, и релиз теряется молча
            # (spec.reader.python-docs).
            authoritative = bool((result.extra or {}).get("authoritative"))
            if authoritative or fuller or better_structure or more_chapters:
                dest, _ = import_file(src, sha1)
                _apply_file(existing, dest, result, sha1)
                # Файл реально заменён — только это событие бампает updated_at.
                # Библиотека сортируется по updated_at; безусловный бамп при
                # no-op перекачке (тот же sha1 / не полнее) поднимал книгу на
                # каждом тике монитора и заглушал сигнал «недавно читал».
                existing.updated_at = utcnow()
                if cur_rich and new_rich > cur_rich:
                    _unfinish_progress(session, existing.id, cur_rich, new_rich)
        if result.source_url and not existing.source_url:
            existing.source_url = result.source_url
        # Книга, которую мы сами качаем с источника, перестаёт быть calibre-ссылкой:
        # site=calibre ей проставил migrate_local_to_refs, но файл вернулся из
        # монитора. Иначе UI показывает бейдж «Calibre» вместо настоящего источника,
        # а reader.py тянет обложку из Calibre вместо обложки книги.
        if existing.site == "calibre" and result.site and existing.file_path:
            existing.site = result.site
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
