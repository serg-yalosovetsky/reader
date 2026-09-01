"""spec.reader.update-pipeline v8 — подписка не должна уходить на источник,
с которого скачать НЕЛЬЗЯ, а backoff обязан останавливать карусель провалов.

Живой случай (2026-09-01), work 58 «Сломанный Меч» / Atlet123:
книга бесплатна на ficbook (78 глав), та же книга платная на author.today.
Монитор смотрел на author.today, каждый тик видел «глав больше, чем у нас»,
качал, получал PaidContentError и писал «Книга платная на author.today…».
fail_count дошёл до 123 при _MAX_FAILS=5 (у соседних записей — 1341 и 581).

Три независимых дефекта, по тесту на каждый.
"""

from __future__ import annotations

from sqlmodel import Session, select

from backend.accounts import dedup, monitor
from backend.app.db.models import Monitored, Work

FICBOOK = "https://ficbook.net/readfic/018abe74-8d53-7f34-a6c1-d29cd3c7446f"
AT_PAID = "https://author.today/work/302190"


def _work(session: Session, **kw) -> Work:
    kw.setdefault("source_url", FICBOOK)
    w = Work(
        title="Сломанный Меч",
        author="Atlet123",
        site="ficbook",
        file_path="",
        file_format="epub",
        sha1="deadbeef",
        chapters_count=77,
        cover_path="",
        description="",
        genres="",
        characters="",
        fandom="",
        rating="",
        status="в процессе",
        words=700649,
        meta_synced=False,
        **kw,
    )
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


# --- Дефект 1: дедуп выбирал канон по счётчику, а не по пригодности источника ---


def test_dedup_keeps_the_source_the_book_actually_came_from(session: Session):
    """Из двух подписок остаётся ficbook (откуда книга скачана), а не платный AT
    с бо́льшим last_seen_chapters."""
    w = _work(session)
    session.add(
        Monitored(
            source_url=FICBOOK,
            work_id=w.id,
            last_seen_chapters=77,
            last_seen_source="ficbook.net",
        )
    )
    session.add(
        Monitored(  # у платного AT счётчик БОЛЬШЕ — раньше этого хватало, чтобы победить
            source_url=AT_PAID,
            work_id=w.id,
            last_seen_chapters=78,
            last_seen_source="author.today",
            fail_count=123,
        )
    )
    session.commit()

    dedup.dedup_monitored(session)

    left = session.exec(select(Monitored).where(Monitored.work_id == w.id)).all()
    assert len(left) == 1
    assert left[0].source_url == FICBOOK, (
        "канон обязан смотреть на источник самой книги; иначе рабочий ficbook "
        "удаляется и остаётся адрес, с которого скачать нельзя"
    )
    # Накопленное состояние группы не теряется.
    assert left[0].last_seen_chapters == 78


def test_dedup_falls_back_to_max_count_when_work_url_is_unknown(session: Session):
    """Книга-ссылка без source_url — старое поведение (макс. счётчик) сохраняется."""
    w = _work(session, source_url="")
    session.add(Monitored(source_url=FICBOOK, work_id=w.id, last_seen_chapters=10))
    session.add(Monitored(source_url=AT_PAID, work_id=w.id, last_seen_chapters=42))
    session.commit()

    dedup.dedup_monitored(session)

    left = session.exec(select(Monitored).where(Monitored.work_id == w.id)).all()
    assert len(left) == 1
    assert left[0].source_url == AT_PAID
    assert left[0].last_seen_chapters == 42


# --- Дефект 2: платная книга на AT предлагалась как «более полный источник» ---


def _stub_at(monkeypatch, *, paid: bool, chapters: int = 78, sample: str = "текст первой главы"):
    from backend.downloaders import authortoday as at

    # Зеркало проверяется ДЕЙСТВИЕМ — пробой первой главы; пустой sample
    # означает «текст не отдают» (18+ без входа, снятие с публикации и т.п.).
    monkeypatch.setattr(at, "fetch_text_sample", lambda url, *a, **kw: sample)
    monkeypatch.setattr(at, "search_work", lambda t, a: AT_PAID)
    monkeypatch.setattr(
        at,
        "fetch_meta",
        lambda url: {
            "title": "Сломанный Меч",
            "author": "Atlet123",
            "annotation": "",
            "paid": paid,
        },
    )
    monkeypatch.setattr(at, "count_chapters", lambda url: chapters)


def _stub_searchfloor(monkeypatch, *, found: bool, chapters: int = 70, title="Сломанный Меч"):
    from backend.downloaders import searchfloor as sf

    monkeypatch.setattr(sf, "search_book", lambda t, a="": ("999" if found else None))
    monkeypatch.setattr(sf, "_book_meta", lambda bid: (title, "Atlet123"))
    monkeypatch.setattr(sf, "count_chapters", lambda url: chapters)


OUR = {"title": "Сломанный Меч", "author": "Atlet123", "annotation": ""}


def test_paid_at_page_is_never_a_mirror(monkeypatch):
    """Платная на AT книга — не зеркало: текста с неё не получить.
    Наличие учётки AT ответом не является — аккаунт ≠ купленная книга."""
    _stub_at(monkeypatch, paid=True)
    assert monitor._check_at_source(OUR) is None


def test_free_at_page_is_still_a_mirror(monkeypatch):
    """Бесплатная на AT книга остаётся кандидатом (регрессия v3)."""
    _stub_at(monkeypatch, paid=False)
    assert monitor._check_at_source(OUR) == (AT_PAID, 78)


def test_free_page_that_gives_no_text_is_not_a_mirror(monkeypatch):
    """Страница говорит «бесплатно», а текст не отдают — это не зеркало.

    Живой случай: work 46 и work 58 на author.today открываются кнопкой
    «Читать книгу» (бесплатны), но при неработающем входе AT отвечает
    `unadulted` на КАЖДУЮ главу. Признаки на странице говорят о ЦЕНЕ,
    а не о ДОСТУПЕ — поэтому решает проба главы, а не разметка."""
    _stub_at(monkeypatch, paid=False, chapters=78, sample="")
    assert monitor._check_at_source(OUR) is None


def test_mirror_probe_passes_at_credentials(monkeypatch):
    """Учётка доезжает до пробы главы — иначе любая 18+ книга считалась бы
    непригодной даже при рабочем входе."""
    from backend.downloaders import authortoday as at

    seen = {}

    def _sample(url, *a, **kw):
        seen["creds"] = kw.get("creds")
        return "текст"

    _stub_at(monkeypatch, paid=False, chapters=78)
    monkeypatch.setattr(at, "fetch_text_sample", _sample)
    monkeypatch.setattr(monitor, "_check_searchfloor_source", lambda our: None)

    monitor._check_mirrors(OUR, ("user", "pass"))
    assert seen["creds"] == ("user", "pass")


# --- Запрос Сержа: искать ВСЕ источники и брать самый полный ---


def test_mirrors_fall_back_to_searchfloor_when_at_is_paid(monkeypatch):
    """Платный AT отбракован, но поиск на этом не заканчивается: бесплатный
    агрегатор всё равно опрашивается."""
    _stub_at(monkeypatch, paid=True, chapters=78)
    _stub_searchfloor(monkeypatch, found=True, chapters=70)
    assert monitor._check_mirrors(OUR) == ("https://searchfloor.org/b/999", 70)


def test_mirrors_pick_the_fullest_of_the_usable_ones(monkeypatch):
    """Из пригодных источников берётся самый полный."""
    _stub_at(monkeypatch, paid=False, chapters=78)
    _stub_searchfloor(monkeypatch, found=True, chapters=70)
    assert monitor._check_mirrors(OUR) == (AT_PAID, 78)

    _stub_at(monkeypatch, paid=False, chapters=60)
    assert monitor._check_mirrors(OUR) == ("https://searchfloor.org/b/999", 70)


def test_mirror_of_another_book_is_rejected(monkeypatch):
    """Тёзка на searchfloor не берётся даже при большем числе глав."""
    _stub_at(monkeypatch, paid=True)
    _stub_searchfloor(monkeypatch, found=True, chapters=500, title="Совсем другая книга")
    assert monitor._check_mirrors(OUR) is None


def test_mirror_search_is_no_longer_limited_to_ficbook_hosts(monkeypatch):
    """Раньше белый список хостов запрещал искать зеркала для AT-подписки —
    именно из-за этого две платные книги набрали 1341 и 581 провал, так ни разу
    и не спросив бесплатные агрегаторы."""
    assert monitor._mirror_eligible("author.today", 46, "Книга") is True
    assert monitor._mirror_eligible("ficbook.net", 58, "Книга") is True
    # без опознаваемой книги искать нечего
    assert monitor._mirror_eligible("ficbook.net", None, "Книга") is False
    assert monitor._mirror_eligible("ficbook.net", 58, "") is False


# --- Дефект 3: backoff не действовал на ветке «на сайте глав больше» ---


def test_backoff_stops_the_carousel_when_the_site_keeps_showing_more(
    session: Session, monkeypatch
):
    """При fail_count >= _MAX_FAILS докачка НЕ запускается, даже если на сайте
    глав больше, чем у нас. Спека требовала этого с v5, код проверял fail_count
    только на ветке залипшего has_update — отсюда живые 1341 / 581 / 123."""
    w = _work(session)
    session.add(
        Monitored(
            source_url=AT_PAID,
            work_id=w.id,
            last_seen_chapters=77,
            last_seen_source="author.today",
            has_update=True,
            fail_count=monitor._MAX_FAILS,  # backoff уже должен был сработать
        )
    )
    session.commit()

    # Сайт стабильно показывает 78 глав — best_cur (78) > seen (77) каждый тик.
    monkeypatch.setattr(monitor, "_count_chapters_task", lambda t: 78)
    monkeypatch.setattr(monitor, "_at_task", lambda t: None)
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)

    calls = []

    def _boom(session_, mon, work_obj, best_url, best_cur):
        calls.append(best_url)
        raise RuntimeError("Книга платная на author.today")

    monkeypatch.setattr(monitor, "_download_and_write", _boom)

    res = monitor.check_all(session, auto_download=True, pull_feeds=False)

    assert calls == [], "докачка не должна запускаться при исчерпанном backoff"
    assert res["downloaded"] == 0
    mon = session.exec(select(Monitored).where(Monitored.work_id == w.id)).one()
    assert mon.fail_count == monitor._MAX_FAILS, "счётчик провалов не должен расти"
    assert mon.has_update is True, "обновление есть — флаг снимать нельзя"


def test_download_still_runs_below_the_backoff_threshold(session: Session, monkeypatch):
    """Ниже порога поведение прежнее — гейт не должен глушить исправные подписки."""
    w = _work(session)
    session.add(
        Monitored(
            source_url=FICBOOK,
            work_id=w.id,
            last_seen_chapters=77,
            last_seen_source="ficbook.net",
            fail_count=monitor._MAX_FAILS - 1,
        )
    )
    session.commit()

    monkeypatch.setattr(monitor, "_count_chapters_task", lambda t: 78)
    monkeypatch.setattr(monitor, "_at_task", lambda t: None)
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)

    calls = []

    def _ok(session_, mon, work_obj, best_url, best_cur):
        calls.append(best_url)
        return {"downloaded": True, "source_used": best_url, "chapters": best_cur}

    monkeypatch.setattr(monitor, "_download_and_write", _ok)

    res = monitor.check_all(session, auto_download=True, pull_feeds=False)

    assert calls == [FICBOOK]
    assert res["downloaded"] == 1
