"""Даты публикации глав (serg/tasks#1080).

Зачем отдельный источник: в файлах книг дат глав нет. FanFicFare кладёт в EPUB
только `chapterurl` и заголовки, а даты отдаёт ОТДЕЛЬНО — в метаданных
(`--json-meta` → `zchapters`), и только в момент запроса к сайту. Проверено на
живых источниках 22.09.2026: forums.sufficientvelocity.com даёт дату с точностью
до секунды, ficbook.net — до минуты, author.today FanFicFare не поддерживает
вовсе (у него свой загрузчик, см. downloaders/authortoday.py).

Поэтому даты собираются при обращении к сайту (проверка обновлений, докачка,
явный запрос оглавления) и складываются в таблицу ChapterMeta. Файл книги не
трогаем: пересобирать EPUB ради даты в заголовке — дорого и портит текст.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from sqlmodel import Session, select

from .db.models import ChapterMeta, Work, utcnow

log = logging.getLogger("reader.chapterdates")

# Формат даты задаёт сам FanFicFare (datechapter_format / datePublished_format),
# и по сайтам он разный: SV отдаёт секунды, ficbook — только минуты. Просим
# единый формат опцией ниже, но разбираем несколько: опция может не долететь до
# адаптера с собственной настройкой, и тогда молчаливый None вместо даты выглядел
# бы как «сайт дат не отдаёт».
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMATS = (DATE_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y")

# Опция для FanFicFare: один формат на все сайты, чтобы разбор не зависел от
# того, какой раздел конфига выиграл. Проценты УДВОЕНЫ намеренно:
# `-o` уезжает в configparser с интерполяцией, и одиночный %% валит ВЕСЬ вызов
# (ValueError: invalid interpolation syntax) — метаданные приходят пустыми,
# и выглядит это как «сайт не отдал дат».
FFF_OPTIONS = {"datechapter_format": DATE_FORMAT.replace("%", "%%")}

# Одновременные сборы одной книги (две вкладки открыли страницу книги) не должны
# дублировать сетевой запрос.
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def parse_date(value: object) -> datetime | None:
    """Строка даты от FanFicFare → datetime. Неразобранное — None и строка в лог."""
    if isinstance(value, datetime):
        return value
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    log.warning("дата главы %r не разобрана ни одним из форматов", text[:40])
    return None


def rows_from_fff_meta(meta: dict) -> list[dict]:
    """`zchapters` из --json-meta → строки для ChapterMeta."""
    rows: list[dict] = []
    for item in meta.get("zchapters") or []:
        # Формат: [номер, {title, url, date, ...}]. Защищаемся от иной формы,
        # чтобы смена версии FanFicFare не роняла сбор дат целиком.
        try:
            number, chap = item
        except (TypeError, ValueError):
            log.warning("неожиданная форма записи главы: %.80r", item)
            continue
        if not isinstance(chap, dict):
            continue
        rows.append(
            {
                "number": int(number or 0),
                "title": (chap.get("title") or "").strip(),
                "url": (chap.get("url") or "").strip(),
                "published_at": parse_date(chap.get("date")),
            }
        )
    return rows


def save_rows(
    session: Session, work_id: int, rows: list[dict], source: str
) -> int:
    """Записать главы книги (перезаписью по номеру). Возвращает число строк с датой."""
    if not rows:
        return 0
    existing = {
        row.number: row
        for row in session.exec(
            select(ChapterMeta).where(ChapterMeta.work_id == work_id)
        ).all()
    }
    with_date = 0
    for row in rows:
        num = row["number"]
        item = existing.get(num)
        if item is None:
            item = ChapterMeta(work_id=work_id, number=num)
            session.add(item)
        item.title = row["title"]
        item.url = row["url"]
        # Дату не затираем пустой: сайт мог перестать её отдавать (смена вёрстки),
        # но однажды узнанная дата публикации меняться не должна.
        if row["published_at"] is not None:
            item.published_at = row["published_at"]
        if item.published_at is not None:
            with_date += 1
        item.source = source
        item.fetched_at = utcnow()
    # Главы, которых у источника больше нет (перенумеровали, удалили), остаются
    # в таблице устаревшими: удалять их — значит терять даты при временном сбое
    # разбора. Лишние строки не видны, потому что оглавление берётся из файла.
    session.commit()
    return with_date


def save_from_meta(
    session: Session, work_id: int, meta: dict, source: str = "fff"
) -> int:
    """Сохранить даты глав из метаданных FanFicFare, если они там есть."""
    rows = rows_from_fff_meta(meta)
    if not rows:
        return 0
    return save_rows(session, work_id, rows, source)


def fetch_for_work(work_id: int) -> int:
    """Сходить к источнику за датами глав и сохранить. Возвращает число дат.

    Блокирующий сетевой запрос — вызывать из пула/фона, не из обработчика.
    """
    from ..accounts import store
    from ..downloaders import fanficfare_engine as fff
    from .db.session import engine

    with _inflight_lock:
        if work_id in _inflight:
            log.info("даты глав %s уже собираются — второй запрос пропущен", work_id)
            return 0
        _inflight.add(work_id)
    try:
        with Session(engine) as session:
            work = session.get(Work, work_id)
            url = (work.source_url or "") if work else ""
            if not url:
                log.info("у работы %s нет source_url — даты глав брать негде", work_id)
                return 0
            creds = store.creds_for_host(session, _host(url))

        if not fff.supports(url) and not _forum(url):
            # Сайт вне FanFicFare (например author.today) — это не ошибка, но и
            # не успех: пусть в логе будет видно, почему дат не появилось.
            log.info("FanFicFare не обслуживает %s — даты глав не собраны", _host(url))
            return 0

        meta = fff.get_meta(url, creds=creds, timeout=180, extra=FFF_OPTIONS)
        if not meta:
            log.warning("метаданные %s пусты — даты глав не собраны", url)
            return 0
        with Session(engine) as session:
            saved = save_from_meta(session, work_id, meta)
        log.info("даты глав %s: сохранено %s", work_id, saved)
        return saved
    finally:
        with _inflight_lock:
            _inflight.discard(work_id)


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def _forum(url: str) -> str:
    """Форумы XenForo FanFicFare поддерживает, но их нет в KNOWN_DOMAINS."""
    host = _host(url)
    return host if host.endswith(("sufficientvelocity.com", "spacebattles.com",
                                  "questionablequesting.com")) else ""


def dates_for_work(session: Session, work_id: int) -> tuple[dict, dict]:
    """(по url → дата, по номеру → дата) — для сопоставления с оглавлением файла."""
    by_url: dict[str, datetime] = {}
    by_number: dict[int, datetime] = {}
    rows = session.exec(
        select(ChapterMeta).where(ChapterMeta.work_id == work_id)
    ).all()
    for row in rows:
        if row.published_at is None:
            continue
        if row.url:
            by_url[row.url] = row.published_at
        by_number[row.number] = row.published_at
    return by_url, by_number


def have_dates(session: Session, work_id: int) -> bool:
    row = session.exec(
        select(ChapterMeta)
        .where(ChapterMeta.work_id == work_id)
        .where(ChapterMeta.published_at.is_not(None))
    ).first()
    return row is not None
