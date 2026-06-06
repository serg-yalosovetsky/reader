"""Мониторинг обновлений отслеживаемых фанфиков.

Надёжный приём: текущее число глав берём через FanFicFare --meta-only (публично,
без логина; с кредами — для закрытого/18+). Сравниваем с last_seen_chapters;
при росте помечаем has_update и (опц.) авто-докачиваем в Calibre/ReadEra.
"""
from __future__ import annotations

import time

from sqlmodel import Session, select

from ..app.db.models import Monitored, Work, utcnow
from ..app.services import register_download
from ..downloaders import chain
from ..downloaders import fanficfare_engine as fff
from . import store


def add_monitor(session: Session, source_url: str, work_id: int | None = None,
                chapters: int = 0) -> Monitored:
    """Поставить фик на отслеживание (идемпотентно по source_url)."""
    mon = session.exec(select(Monitored).where(Monitored.source_url == source_url)).first()
    if mon:
        if work_id and not mon.work_id:
            mon.work_id = work_id
        if chapters:
            mon.last_seen_chapters = max(mon.last_seen_chapters, chapters)
    else:
        mon = Monitored(source_url=source_url, work_id=work_id,
                        last_seen_chapters=chapters)
        session.add(mon)
    session.commit()
    session.refresh(mon)
    return mon


def _chapter_count(url: str, session: Session) -> int | None:
    creds = store.creds_for_host(session, _host(url))
    meta = fff.get_meta(url, creds=creds)
    if not meta:
        return None
    try:
        return int(meta.get("numChapters") or 0)
    except (TypeError, ValueError):
        return None


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


def _check_at_source(mon: "Monitored", session: Session) -> tuple[str, int] | None:
    """Если книга отслеживается на ficbook/fanfics — ищем её же на author.today.
    Возвращает (at_url, at_chapters) если AT-версия найдена, иначе None."""
    from urllib.parse import urlparse
    host = (urlparse(mon.source_url).hostname or "").lower()
    # Только для ficbook / fanfics / fanfiction (AT имеет смысл как альтернатива рус.фанфиков)
    if not any(h in host for h in ("ficbook", "fanfics.me", "fanfiction.net")):
        return None
    if not mon.work_id:
        return None
    w = session.get(Work, mon.work_id)
    if not w or not w.title:
        return None
    from ..downloaders import authortoday as _at
    at_url = _at.search_work(w.title, w.author or "")
    if not at_url:
        return None
    at_cnt = _at.count_chapters(at_url)
    if not at_cnt:
        return None
    return (at_url, at_cnt)



def check_all(session: Session, auto_download: bool = True, pull_feeds: bool = True) -> dict:
    """Проверить обновления: сперва фиды подписок (ставят новые работы на
    отслеживание), затем детект новых глав по каждому отслеживаемому фику."""
    feeds_result = {}
    if pull_feeds:
        from . import feeds  # ленивый импорт — избегаем цикла
        feeds_result = feeds.pull_all(session)

    mons = session.exec(select(Monitored)).all()
    checked = updated = downloaded = 0
    details = []
    for mon in mons:
        if not mon.source_url:
            continue
        cur = _chapter_count(mon.source_url, session)
        mon.last_checked = utcnow()
        checked += 1
        if cur is None:
            session.add(mon); session.commit()
            continue
        # Проверяем author.today как альтернативный источник (может выходить быстрее)
        at_info = _check_at_source(mon, session)
        # Используем источник с большим числом глав
        best_url = mon.source_url
        best_cur = cur or 0
        if at_info:
            at_url, at_cnt = at_info
            if at_cnt > best_cur:
                best_url = at_url
                best_cur = at_cnt
        if best_cur > mon.last_seen_chapters:
            mon.has_update = True
            updated += 1
            detail = {"url": best_url, "from": mon.last_seen_chapters, "to": best_cur}
            if at_info and best_url != mon.source_url:
                detail["alt_source"] = best_url
            if auto_download:
                try:
                    creds = store.creds_for_host(session, _host(best_url))
                    res = chain.fetch(best_url, creds=creds)
                    work = register_download(res, session)
                    mon.work_id = work.id
                    mon.has_update = False  # докачали — обновление применено
                    downloaded += 1
                    detail["downloaded"] = True
                    detail["source_used"] = best_url
                except Exception as e:  # noqa: BLE001 — фон, не валим весь прогон
                    detail["error"] = str(e)[:200]
            details.append(detail)
        mon.last_seen_chapters = max(mon.last_seen_chapters, cur)
        session.add(mon)
        session.commit()
        time.sleep(0.3)  # вежливость к сайтам
    return {"checked": checked, "with_updates": updated,
            "downloaded": downloaded, "feeds": feeds_result, "details": details}


def list_monitored(session: Session) -> list[dict]:
    """Список отслеживаемого с заголовками работ (для UI), без дубликатов."""
    out = []
    seen: set = set()
    for mon in session.exec(select(Monitored)).all():
        title = ""
        if mon.work_id:
            w = session.get(Work, mon.work_id)
            title = w.title if w else ""
        key = (title.strip().lower() or mon.source_url)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": mon.id, "work_id": mon.work_id, "source_url": mon.source_url,
            "title": title, "last_seen_chapters": mon.last_seen_chapters,
            "has_update": mon.has_update, "last_checked": mon.last_checked,
        })
    return out
