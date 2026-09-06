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

import logging

_log = logging.getLogger("reader.monitor")


def add_monitor(
    session: Session, source_url: str, work_id: int | None = None, chapters: int = 0
) -> Monitored:
    """Поставить фик на отслеживание (идемпотентно по source_url)."""
    from ..app import blacklist

    if blacklist.is_blacklisted(session, source_url=source_url):
        return None  # книга в чёрном списке — не возвращаем на отслеживание
    mon = session.exec(
        select(Monitored).where(Monitored.source_url == source_url)
    ).first()
    if mon:
        if work_id and not mon.work_id:
            mon.work_id = work_id
        if chapters:
            _set_seen(mon, chapters, source_url)
    else:
        mon = Monitored(
            source_url=source_url,
            work_id=work_id,
            last_seen_chapters=chapters,
            last_seen_source=_host(source_url) if chapters else "",
        )
        session.add(mon)
    session.commit()
    session.refresh(mon)
    return mon


def _metric_kind(url: str) -> str:
    """В каких единицах данный источник меряет «главы».

    readli отдаёт число СТРАНИЦ читалки, docs.python.org — НОМЕР ВЕРСИИ
    (3.14.7 → 31407), все остальные — число ГЛАВ. Величины несопоставимы, и
    путать их нельзя ни при сравнении, ни при взятии max.

    docs.python.org распознаётся ПОДСТРОКОЙ, а не через `_host`: сюда приходит
    и полный URL, и голый хост из `last_seen_source`, а у голого хоста
    `urlparse` не видит hostname вовсе (вернёт None) — проверка по хосту молча
    дала бы «chapters», единицы разъехались бы, и книга перекачивалась бы на
    каждом тике (spec.reader.python-docs).
    """
    s = (url or "").lower()
    if "docs.python.org" in s:
        return "version"
    return "pages" if _host(url).lower().endswith("readli.net") else "chapters"


def _seen_for(mon: Monitored, url: str) -> int:
    """`last_seen_chapters`, ПРИГОДНЫЙ для сравнения со счётом глав из `url`.

    Если сохранённое число посчитано источником другого класса (подписку
    перенацелили на зеркало), оно бессмысленно: 84 страницы readli «больше»
    любого числа глав author.today, и подписка навсегда считается актуальной
    при недокачанной книге. В этом случае базы сравнения нет — возвращаем 0,
    докачка пройдёт заново и запишет число уже в правильных единицах.
    """
    known = mon.last_seen_source or ""
    if known and _metric_kind(known) != _metric_kind(url):
        return 0
    return mon.last_seen_chapters or 0


def _set_seen(mon: Monitored, value: int, url: str) -> None:
    """Записать last_seen ВМЕСТЕ с единицами, в которых он посчитан.

    max берётся только от сопоставимой базы (см. _seen_for), иначе смена
    источника навсегда запирала бы счётчик на большем «чужом» числе.
    """
    mon.last_seen_chapters = max(_seen_for(mon, url), value or 0)
    mon.last_seen_source = _host(url)


def _chapter_count(url: str, host: str, creds: tuple[str, str] | None) -> int | None:
    """Число глав без записи в БД (creds пробрасываем заранее — функция вызывается
    из потоков, своей сессии у неё нет)."""
    # docs.python.org: «главы» = НОМЕР ВЕРСИИ документации (см. _metric_kind)
    if host.endswith("docs.python.org"):
        from ..downloaders import pythondocs as _pd

        return _pd.count_chapters(url)
    # readli: «главы» = число страниц читалки (пагинация растёт при дописывании)
    if host.endswith("readli.net"):
        from ..downloaders import readli as _rd

        return _rd.count_chapters(url)
    # searchfloor: номер главы из плашки «Последняя глава» на странице книги
    if host.endswith("searchfloor.org"):
        from ..downloaders import searchfloor as _sf

        return _sf.count_chapters(url)
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


def _mirror_eligible(host: str, work_id: int | None, title: str) -> bool:
    """Стоит ли искать альтернативные источники книги.

    Раньше здесь стоял белый список исходных хостов (ficbook / fanfics.me /
    fanfiction.net) — то есть книга, подписанная на author.today, зеркал не
    искала никогда. Живой случай: две платные на AT книги (work 46 и 1662)
    годами падали на каждом тике, так и не спросив бесплатные агрегаторы.
    Нужны только опознаваемая книга (work_id) и название, по которому искать.
    Исключение — документация Python: у неё единственный источник, и поиск
    «зеркал» по названию только тратит запросы (spec.reader.python-docs).
    """
    if host.endswith("docs.python.org"):
        return False
    return bool(work_id) and bool(title)


def _descriptor(w) -> dict:
    """Дескриптор книги для book_identity.same_book (из ORM-Work или None)."""
    from ..app import book_identity as _bi

    return _bi.work_descriptor(w) if w else {"title": "", "author": ""}


def _check_at_source(
    our: dict, at_creds: tuple[str, str] | None = None
) -> tuple[str, int] | None:
    """Ищем ТУ ЖЕ книгу на author.today и ВЕРИФИЦИРУЕМ идентичность (не тёзку!)
    через book_identity.same_book. Раньше матч шёл по названию — и тянулась чужая
    книга-однофамилица с чужой обложкой, плодя дубли и перецепляя монитор.
    (at_url, at_chapters) или None. Чистая сеть, без БД (вызывается из потоков).

    ПЛАТНАЯ на AT книга зеркалом НЕ СЧИТАЕТСЯ никогда: скачать её нельзя,
    а обход платного доступа явно вне scope (spec.reader.best-source). Наличие
    учётки AT ответом не является: аккаунт ≠ купленная книга — залогиненному
    пользователю без покупки AT отвечает `Paid` (spec.reader.update-pipeline v4),
    и анонимный fetch_meta купленное от некупленного всё равно не отличает.
    Прямая AT-подписка этим не затронута: её собственный URL скачивается
    адаптером с учёткой как и раньше — здесь речь только о ПОИСКЕ ЗЕРКАЛА.
    Живой случай: work 58 «Сломанный Меч» — подписка ушла на платный AT и
    набрала fail_count=123 при бесплатном ficbook под рукой."""
    from ..downloaders import authortoday as _at
    from ..app import book_identity as _bi

    at_url = _at.search_work(our.get("title", ""), our.get("author", "") or "")
    if not at_url:
        return None
    at_meta = _at.fetch_meta(at_url)
    if at_meta and at_meta.get("paid"):
        return None  # отдаёт только фрагмент — скачать нечего, это не зеркало
    # ПРОВЕРКА ДЕЙСТВИЕМ: тянем первую главу. Признаки на странице говорят
    # лишь о ЦЕНЕ, а не о ДОСТУПЕ: книга 18+ бесплатна, но без рабочего входа
    # AT отвечает `unadulted` на каждую главу (serg/tasks#319). Пустой сэмпл =
    # зеркало непригодно, какая бы ни была причина. Сэмпл переиспользуется
    # ниже в same_book — лишнего запроса это не стоит.
    sample = _at.fetch_text_sample(at_url, creds=at_creds)
    if not sample.strip():
        return None
    # Аннотации может не быть — тогда идентичность сверяем по тексту (первая глава).
    gta = (
        (lambda: _bi.extract_text_sample(our.get("file_path"), our.get("file_format")))
        if our.get("file_path")
        else None
    )
    if not at_meta or not _bi.same_book(
        our, at_meta, get_text_a=gta, get_text_b=lambda: sample
    ):
        return None  # тёзка/другая книга — не берём как «зеркало»
    at_cnt = at_meta.get("chapters") or _at.count_chapters(at_url)
    if not at_cnt:
        return None
    return (at_url, at_cnt)


def _check_searchfloor_source(our: dict) -> tuple[str, int] | None:
    """То же для searchfloor — бесплатного агрегатора с дешёвым поиском
    и дешёвым счётом глав (плашка «Последняя глава» на /b/<id>).

    Его поиск сырой («берём первый id», без фильтра по автору), поэтому
    идентичность сверяется обязательно: без этого подписку легко увести
    на тёзку или соседний том (spec.reader.best-source v3).
    readli сюда НЕ входит: его поиск умеет только СКАЧАТЬ книгу целиком,
    а его «главы» — страницы пагинации, несравнимые с главами (_metric_kind).
    На шаге докачки readli участвует наравне со всеми (chain.fetch_fullest)."""
    from ..downloaders import searchfloor as _sf
    from ..app import book_identity as _bi

    title = our.get("title", "")
    if not title:
        return None
    bid = _sf.search_book(title, our.get("author", "") or "")
    if not bid:
        return None
    sf_url = f"https://searchfloor.org/b/{bid}"
    sf_title, sf_author = _sf._book_meta(bid)
    if not _bi.same_book(our, {"title": sf_title, "author": sf_author}):
        return None  # тёзка/чужой том
    cnt = _sf.count_chapters(sf_url)
    if not cnt:
        return None  # завершённая книга без плашки — счётчика нет
    return (sf_url, cnt)


# Зонды зеркал, которые можно опросить ДЕШЁВО (поиск + счёт глав, без
# скачивания книги) и чьи единицы — ГЛАВЫ, то есть сравнимы между собой
# и с исходным источником (см. _metric_kind).
_MIRROR_PROBES = (_check_at_source, _check_searchfloor_source)


def _check_mirrors(
    our: dict, at_creds: tuple[str, str] | None = None
) -> tuple[str, int] | None:
    """Опросить ВСЕ дешые зеркала и вернуть САМОЕ ПОЛНОЕ из ПРИГОДНЫХ.

    Пригодное = та же книга (same_book) И с неё реально можно скачать.
    Большее число глав само по себе не делает источник лучше: у платной
    книги на author.today глав всегда больше, а текста не даётся ни одной.
    Чистая сеть, без БД — вызывается из потоков."""
    cands: list[tuple[str, int]] = []
    for probe in _MIRROR_PROBES:
        try:
            r = (
                probe(our, at_creds)
                if probe is _check_at_source
                else probe(our)
            )
        except Exception:  # noqa: BLE001 — одно лежачее зеркало не валит остальные
            r = None
        if r:
            cands.append(r)
    if not cands:
        return None
    return max(cands, key=lambda c: c[1])


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


# Зонд зеркал ходит на ЧУЖИЕ сервисы по ВСЕМ подпискам. На тике в 20 минут и
# 70 подписках это тысячи запросов в сутки на каждый агрегатор — а трафик к
# author.today идёт через резидентный egress (домашняя нода), то есть бан прилетит
# на домашний IP. Наличие книги на зеркале меняется редко, поэтому результат
# кэшируется. Детект обновлений от этого не страдает: число глав СВОЕГО
# источника считается каждый тик, зеркало — дополнение.
_MIRROR_TTL_SEC = 6 * 3600
_mirror_cache: dict[int, tuple[float, tuple[str, int] | None]] = {}


def _at_task(task: dict) -> tuple[str, int] | None:
    if not _mirror_eligible(task["host"], task["work_id"], task["title"]):
        return None
    wid = task["work_id"]
    hit = _mirror_cache.get(wid)
    if hit and (time.time() - hit[0]) < _MIRROR_TTL_SEC:
        return hit[1]
    try:
        res = _check_mirrors(task["our"], task.get("at_creds"))
    except Exception:  # noqa: BLE001
        return None  # неудачу НЕ кэшируем: следующий тик попробует снова
    _mirror_cache[wid] = (time.time(), res)
    return res


def reset_mirror_cache() -> None:
    """Сбросить кэш зонда зеркал (ручная проверка = «посмотри заново»)."""
    _mirror_cache.clear()


_AT_COVER_ELIGIBLE = ("ficbook.net", "readli.net", "searchfloor.org", "fanfics.me")


def _fetch_at_cover_bytes(source_url: str, title: str, author: str) -> bytes | None:
    """Скачать обложку с author.today — чистая сеть, без сессии и транзакций."""
    from urllib.parse import urlparse
    from ..app import covers as _cov
    from ..downloaders import authortoday as _at

    host = (urlparse(source_url).hostname or "").lower()
    if not any(host.endswith(e) for e in _AT_COVER_ELIGIBLE):
        return None
    if not title:
        return None
    try:
        at_url = _at.search_work(title, author or "")
        if not at_url:
            return None
        img = _cov.fetch_cover_bytes(at_url)
        return img if img and len(img) >= 10_000 else None
    except Exception:  # noqa: BLE001
        return None


def _apply_at_cover(session: Session, work: Work, cover_bytes: bytes | None) -> None:
    """Сохранить обложку и пометить work грязным (без commit — вызывающий коммитит)."""
    if not cover_bytes:
        return
    from ..app import covers as _cov

    p = _cov.save_cover_bytes(cover_bytes, work.sha1)
    if p:
        work.cover_path = str(p)
        session.add(work)


def _download_and_write(
    session: Session,
    mon: "Monitored",
    work_obj: "Work | None",
    best_url: str,
    best_cur: int,
) -> dict:
    """Скачать книгу + обложку, записать результат.

    Структура гарантирует короткие транзакции:
      1. Читаем creds (быстро) -> commit -> никакой открытой txn.
      2. Вся сеть (download + cover) вне любой транзакции.
      3. Быстрая запись: register_download (внутренний commit) +
         cover_path + mon — один финальный commit.
    """
    # snapshot plain data от ORM-объектов до expires
    src_url = mon.source_url
    mon_id = mon.id
    title = work_obj.title if work_obj else ""
    author = (work_obj.author if work_obj else "") or ""
    # Снимок дескриптора ДО expires — для сверки идентичности кандидатов-зеркал.
    descriptor = {
        "title": title,
        "author": author,
        "annotation": (work_obj.description if work_obj else "") or "",
        "file_path": work_obj.file_path if work_obj else None,
        "file_format": work_obj.file_format if work_obj else None,
    }
    creds = store.creds_for_host(session, _host(best_url))

    # Закрываем ВСЕ pending-изменения — после этого нет открытой транзакции
    session.commit()

    # Сеть — транзакций нет вообще
    cover_bytes = _fetch_at_cover_bytes(src_url, title, author)
    # Выбираем САМЫЙ ПОЛНЫЙ источник: качаем текущий + зеркала (AT/searchfloor/
    # readli), верифицируем same_book, берём вариант с бо́льшим объёмом текста.
    # Фолбэк на прямую докачку, если авто-выбор ничего не дал.
    res = chain.fetch_fullest(
        title, author, primary_url=best_url, creds=creds, descriptor=descriptor
    ) or chain.fetch(best_url, creds=creds)

    # Быстрая запись (< секунды)
    work = register_download(res, session)  # внутренний commit
    _apply_at_cover(session, work, cover_bytes)  # session.add(work) если есть обложка
    # Полный fb2-зеркала (searchfloor) не разбит на главы → num_chapters=0, и
    # register_download оставляет старый счётчик. Проставим известное из монитора
    # число глав (best_cur), иначе UI показывает устаревший «N гл».
    # Заполняем счётчик глав из монитора ТОЛЬКО если адаптер его не дал (searchfloor
    # отдаёт цельный FB2, num_chapters=0). Иначе (readli/AT — реальные главы) не
    # затираем настоящее число «страничным» best_cur (у readli это число страниц).
    if best_cur and not (work.chapters_count or 0):
        work.chapters_count = best_cur
        session.add(work)

    mon = session.get(Monitored, mon_id)  # re-fetch после commit
    mon.work_id = work.id
    # last_seen двигаем ТОЛЬКО по материализованному контенту: файл мог прийти из
    # отставшего зеркала или свежая глава платная. Считаем реальные главы в
    # применённом файле; если их < best_cur — обещанная глава не докачана, has_update
    # оставляем и штрафуем (backoff погасит карусель), иначе глава теряется навсегда
    # (best_cur<=last_seen → «актуально»). materialized==0 (не смогли посчитать, напр.
    # page-blob) → доверяем best_cur, старое поведение.
    # Полнота считается ЧИСЛОМ ГЛАВ В ФАЙЛЕ (count_sections), а не числом глав с
    # «хорошими» названиями (_real_chapters): последний выбрасывает «Часть N», а на
    # ficbook это настоящие авторские названия — полностью скачанная книга выглядела
    # как «2 гл. из 14», has_update залипал, fail_count рос до сотен.
    from ..app.services import count_sections as _cs

    materialized = (
        _cs(work.file_path, work.file_format, book_title=work.title or "")
        if work.file_path
        else 0
    )
    # Сравнивать можно только однородные величины. У readli _chapter_count считает
    # СТРАНИЦЫ читалки (80), а в файле — ГЛАВЫ (21), причём адаптер сам обходит
    # всю пагинацию. Без этой поправки полностью скачанная книга вечно числится
    # недокачанной: fail_count растёт и через _MAX_FAILS подписка выпадает
    # из авторетрая (живой случай: «Вечно голодный студент 9», 21 гл. из 80 стр.).
    # Разнородна ЛЮБАЯ метрика, кроме глав: у readli это страницы пагинации, у
    # docs.python.org — номер версии (31407). Сравнение с числом секций в файле
    # там бессмысленно и приводит к вечному has_update: книга качалась бы заново
    # на каждом тике, а fail_count рос бы до сотен.
    _heterogeneous = _metric_kind(best_url) != "chapters"
    got_all = (materialized == 0) or _heterogeneous or (materialized >= best_cur)
    mon.last_checked = utcnow()
    if got_all:
        mon.has_update = False
        mon.fail_count = 0
        mon.last_error = None
        _set_seen(mon, best_cur, best_url)
    else:
        mon.has_update = True
        mon.fail_count = (mon.fail_count or 0) + 1
        mon.last_error = (
            f"докачано {materialized} гл., на сайте {best_cur} — глава недоступна?"
        )
        # materialized — ГЛАВЫ из файла; сюда попадаем только при chapters-метрике
        # (разнородные источники отсечены _heterogeneous выше), единицы сходятся.
        _set_seen(mon, materialized, best_url)
    session.add(mon)
    # Синхронизируем все дубликаты monitored для того же work_id
    for dup in session.exec(
        select(Monitored).where(Monitored.work_id == work.id, Monitored.id != mon_id)
    ).all():
        dup.has_update = mon.has_update
        # Счётчик переносится только между источниками ОДНОГО класса: дубли той
        # же книги вполне могут смотреть на readli (страницы) и на AT (главы),
        # и перенос числа между ними запер бы дубль ровно так же, как основную
        # запись (см. _metric_kind).
        if _metric_kind(dup.source_url) == _metric_kind(mon.last_seen_source or best_url):
            dup.last_seen_chapters = max(dup.last_seen_chapters, mon.last_seen_chapters)
            dup.last_seen_source = mon.last_seen_source
        session.add(dup)
    session.commit()  # один быстрый commit: cover_path + mon + дубликаты

    return {"downloaded": got_all, "source_used": best_url, "chapters": materialized}


# После стольких подряд неудач автодокачки подписка выводится из авторетрая
# (карусель «качаем→падаем» каждый тик душила VPS и бампала updated_at).
# Сбрасывается ручной проверкой (POST /api/monitored/check) или успехом.
_MAX_FAILS = 5


def check_all(
    session: Session,
    auto_download: bool = True,
    pull_feeds: bool = True,
    progress_cb=None,
    update_cb=None,
    only_pending: bool = False,
) -> dict:
    """Проверить обновления: сперва фиды подписок (ставят новые работы на
    отслеживание), затем детект новых глав по каждому отслеживаемому фику.
    only_pending=True — только докачать книги с has_update=True (без счёта глав)."""
    feeds_result = {}
    if pull_feeds:
        from . import feeds  # ленивый импорт — избегаем цикла

        feeds_result = feeds.pull_all(session)

    from ..app import blacklist as _bl

    # --- ФАЗА 0: чёрный список (последовательно, мутирует БД) ---
    survivors: list[tuple] = []
    for mon in session.exec(select(Monitored).order_by(Monitored.id)).all():
        if not mon.source_url:
            continue
        _w = session.get(Work, mon.work_id) if mon.work_id else None
        if _bl.is_blacklisted(
            session,
            source_url=mon.source_url,
            title=(_w.title if _w else ""),
            author=(_w.author if _w else ""),
        ):
            session.delete(mon)
            session.commit()
            continue
        survivors.append((mon, _w))

    # В режиме only_pending обрабатываем только уже помеченные книги
    if only_pending:
        survivors = [(mon, w) for mon, w in survivors if mon.has_update]

    # --- ФАЗА 1: read-only сбор (число глав + альтернатива на AT) ---
    creds_cache: dict[str, tuple[str, str] | None] = {}
    # Учётка AT нужна ЛЮБОЙ подписке, а не только AT-шной: без неё проба
    # главы объявит непригодным любое 18+ зеркало.
    at_creds = store.creds_for_host(session, "author.today")
    tasks = []
    for mon, _w in survivors:
        host = _host(mon.source_url).lower()
        if host not in creds_cache:
            creds_cache[host] = store.creds_for_host(session, host)
        tasks.append(
            {
                "mon_id": mon.id,
                "url": mon.source_url,
                "host": host,
                "creds": creds_cache[host],
                "work_id": mon.work_id,
                "title": _w.title if _w else "",
                "author": (_w.author if _w else "") or "",
                "our": _descriptor(_w),
                "at_creds": at_creds,
                "has_update": mon.has_update,
                "last_seen": mon.last_seen_chapters,
            }
        )
    # 1a) Счёт глав — ПОСЛЕДОВАТЕЛЬНО (ficbook через cloudscraper, нельзя смешивать
    #     с пулом httpx — душат друг друга на маленьком VPS).
    #     Пропускаем если обновление уже известно (has_update=True).
    cur_by: dict[int, int | None] = {}
    total_tasks = len(tasks)
    _t0 = time.time()
    for i, t in enumerate(tasks):
        if progress_cb:
            progress_cb(i + 1, total_tasks, t["title"] or t["url"], t["host"])
        if t.get("has_update") and not t.get("last_seen"):
            # Initial/сирота: сравнивать не с чем — прямая докачка в фазе 2.
            cur_by[t["mon_id"]] = None
        else:
            # Для has_update с известным last_seen СЧИТАЕМ главы: ficbook-лента
            # метит любую активность автора, и без сверки ложный флаг гонял
            # полную перекачку каждый тик.
            cur_by[t["mon_id"]] = _count_chapters_task(t)
    _t_count = time.time() - _t0
    # 1b) Поиск зеркал — ПАРАЛЛЕЛЬНО (анонимные httpx-запросы).
    _t1 = time.time()
    at_by: dict[int, tuple[str, int] | None] = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=_AT_WORKERS) as ex:
            for t, at_info in zip(tasks, ex.map(_at_task, tasks)):
                at_by[t["mon_id"]] = at_info
    # Длительность фаз — в лог. Без неё регрессия тика (залипший агрегатор,
    # выросшее число подписок) невидима: тик просто начинает налезать на
    # следующий, и ни одна ошибка нигде не появляется.
    _cached = sum(
        1
        for t in tasks
        if (_h := _mirror_cache.get(t["work_id"])) and _h[0] < _t1
    )
    _log.info(
        "check_all: подписок=%d счёт_глав=%.1fс зеркала=%.1fс (из кэша %d)",
        len(tasks), _t_count, time.time() - _t1, _cached,
    )
    counts: dict[int, tuple[int | None, tuple[str, int] | None]] = {
        t["mon_id"]: (cur_by.get(t["mon_id"]), at_by.get(t["mon_id"])) for t in tasks
    }

    # --- ФАЗА 2: решения, загрузки, запись ---
    # Каждая итерация: snapshot -> commit -> сеть -> быстрая запись.
    # Транзакция открыта только во время DB-операций (миллисекунды).
    checked = updated = downloaded = 0
    details = []
    for mon, _w in survivors:
        cur, at_info = counts.get(mon.id, (None, None))
        checked += 1
        if cur is None:
            if (
                not (mon.has_update and auto_download)
                or (mon.fail_count or 0) >= _MAX_FAILS
            ):
                mon.last_checked = utcnow()
                session.add(mon)
                session.commit()
                continue
            # Known update от notifications — докачиваем без счёта глав
            best_url = mon.source_url
            best_cur = _seen_for(mon, best_url)
            if at_info:
                at_url, at_cnt = at_info
                best_url = at_url
                best_cur = max(_seen_for(mon, at_url), at_cnt)
            updated += 1
            detail: dict = {
                "url": best_url,
                "from": _seen_for(mon, best_url),
                "source": "notifications",
            }
            if at_info and best_url != mon.source_url:
                detail["alt_source"] = best_url
            try:
                dl = _download_and_write(session, mon, _w, best_url, best_cur)
                detail.update(dl)
                downloaded += 1
            except Exception as e:  # noqa: BLE001
                detail["error"] = str(e)[:200]
                _log.warning("reader download error: %s", e)
                mon = session.get(Monitored, mon.id)
                if mon is None:  # мог быть удалён параллельным дедупом
                    details.append(detail)
                    continue
                mon.fail_count = (mon.fail_count or 0) + 1
                mon.last_error = str(e)[:300]
                mon.last_checked = utcnow()
                session.add(mon)
                session.commit()
            details.append(detail)
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
        # Сравниваем ТОЛЬКО с сопоставимой базой: если last_seen посчитан
        # источником другого класса, он не «уже видели», а чужая величина.
        seen = _seen_for(mon, best_url)
        if mon.has_update and best_cur and best_cur <= seen:
            # Ложный флаг (лента метит любую активность автора): на сайте глав
            # не больше, чем уже видели — снимаем без перекачки.
            mon.has_update = False
            mon.fail_count = 0
            mon.last_error = None
            session.add(mon)
        if (
            best_cur > seen
            or needs_initial
            or (mon.has_update and auto_download and (mon.fail_count or 0) < _MAX_FAILS)
        ):
            mon.has_update = True
            updated += 1
            if update_cb:
                update_cb(updated)
            detail = {"url": best_url, "from": seen, "to": best_cur}
            if at_info and best_url != mon.source_url:
                detail["alt_source"] = best_url
            # Backoff действует НА ВСЕХ ветках, а не только там, где причина —
            # залипший has_update. Рост числа глав на сайте (best_cur > seen) сам по
            # себе не делает источник скачиваемым: у платной книги на author.today
            # глав всегда больше, чем у нас, и без этого гейта подписка качалась и
            # падала каждый тик: живые fail_count 1341 / 581 / 123 при _MAX_FAILS=5.
            # Флаг has_update остаётся — обновление есть, просто не берётся;
            # ручная проверка снимает backoff (reset_fail_counters).
            _backed_off = (mon.fail_count or 0) >= _MAX_FAILS
            if _backed_off:
                detail["skipped"] = f"backoff: {mon.fail_count} неудач подряд"
            if auto_download and not _backed_off:
                try:
                    dl = _download_and_write(session, mon, _w, best_url, best_cur)
                    detail.update(dl)
                    downloaded += 1
                    details.append(detail)
                    continue  # mon уже записан внутри _download_and_write
                except Exception as e:  # noqa: BLE001
                    detail["error"] = str(e)[:200]
                    _log.warning("reader download error: %s", e)
                    _mf = session.get(Monitored, mon.id)
                    if _mf is not None:
                        _mf.fail_count = (_mf.fail_count or 0) + 1
                        _mf.last_error = str(e)[:300]
                        session.add(_mf)
            details.append(detail)
        # last_seen двигаем только если докачка удалась или обновлений нет
        mon = session.get(Monitored, mon.id)  # re-fetch после возможного commit
        if mon is None:  # мог быть удалён параллельным дедупом
            continue
        if not mon.has_update:
            # cur посчитан по mon.source_url — в его единицах и записываем.
            _set_seen(mon, cur or 0, mon.source_url)
        mon.last_checked = utcnow()
        session.add(mon)
        session.commit()
    return {
        "checked": checked,
        "with_updates": updated,
        "downloaded": downloaded,
        "feeds": feeds_result,
        "details": details,
    }


def check_one(session: Session, work_id: int, auto_download: bool = True) -> dict:
    """Проверить обновления для одной книги (по work_id). Синхронно."""
    from ..app import blacklist as _bl

    mon = session.exec(select(Monitored).where(Monitored.work_id == work_id)).first()
    if not mon:
        return {"error": "not_monitored"}

    _w = session.get(Work, work_id)
    host = _host(mon.source_url).lower()

    if _bl.is_blacklisted(
        session,
        source_url=mon.source_url,
        title=(_w.title if _w else ""),
        author=(_w.author if _w else ""),
    ):
        return {"error": "blacklisted"}

    # Читаем creds (быстро) — потом commit, уходим в сеть
    creds = store.creds_for_host(session, host)
    session.commit()  # закрыли любую pending-txn

    # Сеть — без транзакций
    cur = _chapter_count(mon.source_url, host, creds)

    at_info = None
    if _mirror_eligible(host, work_id, _w.title if _w else ""):
        try:
            at_info = _check_mirrors(
                _descriptor(_w), store.creds_for_host(session, "author.today")
            )
        except Exception:
            pass

    best_url = mon.source_url
    best_cur = cur or 0
    if at_info:
        at_url, at_cnt = at_info
        if at_cnt > best_cur:
            best_url = at_url
            best_cur = at_cnt

    mon = session.get(Monitored, mon.id)  # re-fetch после commit
    seen = _seen_for(mon, best_url)  # только сопоставимая база, см. _metric_kind
    has_new = (
        (best_cur > seen) or (not mon.work_id and best_cur > 0) or mon.has_update
    )
    detail: dict = {
        "has_update": has_new,
        "chapters_seen": seen,
        "chapters_found": best_cur,
    }

    if has_new and auto_download:
        try:
            dl = _download_and_write(session, mon, _w, best_url, best_cur)
            detail.update(dl)
            return detail
        except Exception as e:  # noqa: BLE001
            detail["error"] = str(e)[:200]
            _log.warning("reader download error: %s", e)

    # Нет загрузки или ошибка — быстрая запись только mon
    mon = session.get(Monitored, mon.id)
    if has_new:
        mon.has_update = True
    else:
        # cur посчитан по mon.source_url — в его единицах и записываем.
        _set_seen(mon, cur or 0, mon.source_url)
    mon.last_checked = utcnow()
    session.add(mon)
    session.commit()
    return detail


def reset_fail_counters(session: Session) -> int:
    """Снять backoff со всех подписок (ручной запуск проверки = новая попытка)."""
    reset_mirror_cache()  # человек просит проверить заново — значит и зеркала тоже
    n = 0
    for mon in session.exec(select(Monitored).where(Monitored.fail_count > 0)).all():
        mon.fail_count = 0
        session.add(mon)
        n += 1
    if n:
        session.commit()
    return n


def list_monitored(session: Session) -> list[dict]:
    """Список отслеживаемого с заголовками работ (для UI), без дубликатов."""
    out = []
    seen: set = set()
    for mon in session.exec(select(Monitored)).all():
        title = ""
        if mon.work_id:
            w = session.get(Work, mon.work_id)
            title = w.title if w else ""
        key = title.strip().lower() or mon.source_url
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": mon.id,
                "work_id": mon.work_id,
                "source_url": mon.source_url,
                "title": title,
                "last_seen_chapters": mon.last_seen_chapters,
                "has_update": mon.has_update,
                "last_checked": mon.last_checked,
                "fail_count": mon.fail_count or 0,
                "last_error": mon.last_error,
            }
        )
    return out
