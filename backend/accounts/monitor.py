"""Мониторинг обновлений отслеживаемых фанфиков.

Надёжный приём: текущее число глав берём через FanFicFare --meta-only (публично,
без логина; с кредами — для закрытого/18+). Сравниваем с last_seen_chapters;
при росте помечаем has_update и (опц.) авто-докачиваем в Calibre/ReadEra.

Скорость (check_all): самый дорогой кусок — поиск альтернативы на author.today
(_check_at_source, ~0.8с на каждый ficbook-фикл, в сумме ~47с) — распараллелен
(запросы к AT анонимные, их безопасно гонять пулом). Счёт глав ficbook остаётся
на FanFicFare и идёт ПОСЛЕДОВАТЕЛЬНО, отдельной фазой: одновременный доступ к
ficbook (anti-bot) и пул httpx душат друг друга, а агрессивный in-process
cloudscraper ficbook быстро банит. Все записи в БД и докачки — потом,
последовательно в главной сессии (никаких потоков с БД).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from sqlmodel import Session, select

from ..app.db.models import Monitored, Work, utcnow
from ..app.services import register_download
from ..downloaders import chain
from ..downloaders import fanficfare_engine as fff
from . import store


def add_monitor(session: Session, source_url: str, work_id: int | None = None,
                chapters: int = 0) -> Monitored:
    """Поставить фик на отслеживание (идемпотентно по source_url)."""
    from ..app import blacklist
    if blacklist.is_blacklisted(session, source_url=source_url):
        return None  # книга в чёрном списке — не возвращаем на отслеживание
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


def _chapter_count(url: str, host: str, creds: tuple[str, str] | None) -> int | None:
    """Число глав без записи в БД (creds пробрасываем заранее — функция вызывается
    из потоков, своей сессии у неё нет)."""
    # searchfloor/readli: нет FanFicFare-адаптера — не мониторим по главам
    if host.endswith(("searchfloor.org", "readli.net")):
        return None
    # author.today: FanFicFare не поддерживает — используем наш адаптер
    if host.endswith("author.today"):
        from ..downloaders import authortoday as _at
        return _at.count_chapters(url)
    # ficbook: считаем через FanFicFare (свой cloudscraper в подпроцессе). Лёгкий
    # in-process cloudscraper-счётчик пробовали — DDoS-Guard жёстко блокирует
    # переиспользуемую сессию после ~десятка запросов, так что это тупик.
    meta = fff.get_meta(url, creds=creds)
    if host.endswith("ficbook.net"):
        time.sleep(0.25)  # вежливость к DDoS-Guard: он тригеристый, не частим
    if not meta:
        return None
    try:
        return int(meta.get("numChapters") or 0)
    except (TypeError, ValueError):
        return None


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""


def _at_eligible(host: str, work_id: int | None, title: str) -> bool:
    """Стоит ли искать альтернативу на author.today (AT — быстрый рус.источник)."""
    if not any(h in host for h in ("ficbook", "fanfics.me", "fanfiction.net")):
        return False
    return bool(work_id) and bool(title)


def _check_at_source(title: str, author: str) -> tuple[str, int] | None:
    """Ищем работу на author.today по названию. (at_url, at_chapters) или None.
    Чистая сеть, без БД — вызывается из потоков. Запросы к AT анонимные."""
    from ..downloaders import authortoday as _at
    at_url = _at.search_work(title, author or "")
    if not at_url:
        return None
    at_cnt = _at.count_chapters(at_url)
    if not at_cnt:
        return None
    return (at_url, at_cnt)


# Конкурентность поиска на author.today (анонимные httpx-запросы, безопасно).
# 5 проверено (≈9.5с на 54). Выше не берём: _at._get ретраит 4× с backoff, и при
# рейт-лимите AT на больших конкурентностях получаем каскад ретраев — медленнее и
# агрессивнее. Гоняем ОТДЕЛЬНО от ficbook-счёта (одновременность их душит).
_AT_WORKERS = 5


# ВАЖНО: счёт глав ficbook (FanFicFare+cloudscraper) и пул AT-запросов (httpx)
# НЕЛЬЗЯ гонять одновременно — эмпирически душат друг друга (контеншн CPU/сети на
# маленьком VPS: anti-bot ficbook + конкурентные httpx), общий прогон раздувается
# ~втрое. Поэтому фазы РАЗДЕЛЕНЫ: сперва счёт глав, потом отдельный пул на at_source.
def _count_chapters_task(task: dict) -> int | None:
    try:
        return _chapter_count(task["url"], task["host"], task["creds"])
    except Exception:  # noqa: BLE001 — фон, не валим прогон
        return None


def _at_task(task: dict) -> tuple[str, int] | None:
    if not _at_eligible(task["host"], task["work_id"], task["title"]):
        return None
    try:
        return _check_at_source(task["title"], task["author"])
    except Exception:  # noqa: BLE001
        return None



def _refresh_at_cover(work: Work, session: Session) -> None:
    """После скачивания обновить обложку с author.today (ficbook/readli/searchfloor дают логотип)."""
    from urllib.parse import urlparse
    from ..app import covers as _cov
    from ..downloaders import authortoday as _at

    _ELIGIBLE = ("ficbook.net", "readli.net", "searchfloor.org", "fanfics.me")
    # Ищем source_url по monitored
    mon = session.exec(
        __import__('sqlmodel', fromlist=['select']).select(
            __import__('backend.app.db.models', fromlist=['Monitored']).Monitored
        ).where(
            __import__('backend.app.db.models', fromlist=['Monitored']).Monitored.work_id == work.id
        )
    ).first()
    src_url = mon.source_url if mon else ""
    host = (urlparse(src_url).hostname or "").lower()
    if not any(host.endswith(e) for e in _ELIGIBLE):
        return
    if not work.title:
        return
    try:
        at_url = _at.search_work(work.title, work.author or "")
        if not at_url:
            return
        img = _cov.fetch_cover_bytes(at_url)
        if not img or len(img) < 10000:
            return
        p = _cov.save_cover_bytes(img, work.sha1)
        if p:
            work.cover_path = str(p)
            session.add(work)
            session.commit()
    except Exception:  # noqa: BLE001
        pass


def check_all(session: Session, auto_download: bool = True, pull_feeds: bool = True) -> dict:
    """Проверить обновления: сперва фиды подписок (ставят новые работы на
    отслеживание), затем детект новых глав по каждому отслеживаемому фику."""
    feeds_result = {}
    if pull_feeds:
        from . import feeds  # ленивый импорт — избегаем цикла
        feeds_result = feeds.pull_all(session)

    from ..app import blacklist as _bl

    # --- ФАЗА 0: чёрный список (последовательно, мутирует БД) ---
    survivors: list[tuple] = []  # (mon, work_or_None)
    for mon in session.exec(select(Monitored)).all():
        if not mon.source_url:
            continue
        _w = session.get(Work, mon.work_id) if mon.work_id else None
        if _bl.is_blacklisted(session, source_url=mon.source_url,
                              title=(_w.title if _w else ""),
                              author=(_w.author if _w else "")):
            session.delete(mon); session.commit(); continue
        survivors.append((mon, _w))

    # --- ФАЗА 1: read-only сбор (число глав + альтернатива на AT) ---
    # Всё нужное для сети вытаскиваем заранее в plain-dict — потоки в БД не лезут.
    creds_cache: dict[str, tuple[str, str] | None] = {}
    tasks = []
    for mon, _w in survivors:
        host = _host(mon.source_url).lower()
        if host not in creds_cache:
            creds_cache[host] = store.creds_for_host(session, host)
        tasks.append({
            "mon_id": mon.id, "url": mon.source_url, "host": host,
            "creds": creds_cache[host], "work_id": mon.work_id,
            "title": _w.title if _w else "", "author": (_w.author if _w else "") or "",
            "has_update": mon.has_update,
        })
    # 1a) Счёт глав — ПОСЛЕДОВАТЕЛЬНО (ficbook через cloudscraper, нельзя смешивать
    #     с пулом httpx — см. коммент у _count_chapters_task).
    #     Пропускаем если обновление уже известно от ficbook notifications (has_update=True) —
    #     это экономит N FanFicFare-запросов и укладывается в nginx-таймаут.
    cur_by: dict[int, int | None] = {}
    for t in tasks:
        if t.get("has_update"):
            cur_by[t["mon_id"]] = None  # skip; в фазе 2 — прямая докачка
        else:
            cur_by[t["mon_id"]] = _count_chapters_task(t)
    # 1b) Поиск на author.today — ПАРАЛЛЕЛЬНО (анонимные httpx-запросы).
    at_by: dict[int, tuple[str, int] | None] = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=_AT_WORKERS) as ex:
            for t, at_info in zip(tasks, ex.map(_at_task, tasks)):
                at_by[t["mon_id"]] = at_info
    counts: dict[int, tuple[int | None, tuple[str, int] | None]] = {
        t["mon_id"]: (cur_by.get(t["mon_id"]), at_by.get(t["mon_id"])) for t in tasks
    }

    # --- ФАЗА 2: решения, докачки, запись — последовательно в главной сессии ---
    checked = updated = downloaded = 0
    details = []
    for mon, _w in survivors:
        cur, at_info = counts.get(mon.id, (None, None))
        mon.last_checked = utcnow()
        checked += 1
        if cur is None:
            if not (mon.has_update and auto_download):
                session.add(mon); session.commit()
                continue
            # Known update от notifications — докачиваем без счёта глав
            best_url = mon.source_url
            best_cur = mon.last_seen_chapters  # реальный счётчик неизвестен — не трогаем
            at_info = counts.get(mon.id, (None, None))[1]
            if at_info:
                at_url, at_cnt = at_info
                best_url = at_url
                best_cur = max(best_cur, at_cnt)
            updated += 1
            detail = {"url": best_url, "from": mon.last_seen_chapters, "source": "notifications"}
            if at_info and best_url != mon.source_url:
                detail["alt_source"] = best_url
            try:
                creds = store.creds_for_host(session, _host(best_url))
                res = chain.fetch(best_url, creds=creds)
                work = register_download(res, session)
                mon.work_id = work.id
                mon.has_update = False
                downloaded += 1
                detail["downloaded"] = True
                _refresh_at_cover(work, session)
            except Exception as e:  # noqa: BLE001
                detail["error"] = str(e)[:200]
            details.append(detail)
            session.add(mon)
            session.commit()
            continue
        # Используем источник с большим числом глав
        best_url = mon.source_url
        best_cur = cur or 0
        if at_info:
            at_url, at_cnt = at_info
            if at_cnt > best_cur:
                best_url = at_url
                best_cur = at_cnt
        needs_initial = not mon.work_id and best_cur > 0
        if best_cur > mon.last_seen_chapters or needs_initial or (mon.has_update and auto_download):
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
                    # Обновляем обложку с AT если книга из ficbook/readli/searchfloor
                    _refresh_at_cover(work, session)
                except Exception as e:  # noqa: BLE001 — фон, не валим весь прогон
                    detail["error"] = str(e)[:200]
            details.append(detail)
        # last_seen двигаем только если докачка удалась (has_update сброшен) либо
        # обновлений нет — иначе фик не «застрянет» при ошибке докачки (будет ретрай).
        if not mon.has_update:
            mon.last_seen_chapters = max(mon.last_seen_chapters, cur)
        session.add(mon)
        session.commit()
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
