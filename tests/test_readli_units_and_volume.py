"""spec.reader.update-pipeline v11 — «Вечно голодный студент 10» (2026-09-15).

Книга на author.today ушла в платную подписку (с 14-й главы `Paid`), бесплатная
полная копия лежала на readli, но автообновление её не находило. Два дефекта,
по тесту на каждый класс.

1) Единицы readli терялись на ГОЛОМ хосте. `last_seen_source` хранит «readli.net»,
   а `_metric_kind` узнавал readli через `_host()`, который у голого хоста пуст, —
   ответ «chapters». Следствия: страница книги «25 гл. из 109» (страницы
   подписаны главами), а `_seen_for` объявлял базу несопоставимой и отдавал 0,
   то есть книга с readli считалась обновлённой на каждом тике. Та же ловушка,
   что уже была закрыта для docs.python.org, — но только для него.

2) Поиск readli (`/srch/`) не знает номера тома и из равных по похожести
   названий берёт первое: на запрос тома 10 вернулся том 2. А тома 10 в выдаче
   нет вовсе — он есть только на странице автора.
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.accounts import monitor
from backend.app.db.models import Monitored
from backend.downloaders import readli

RL = "https://readli.net/chitat-online/?b=1387674"
TITLE = "Вечно голодный студент"


# --- Дефект 1: единицы измерения на голом хосте ---


def test_metric_kind_bare_readli_host_is_pages():
    assert monitor._metric_kind("readli.net") == "pages"
    assert monitor._metric_kind(RL) == "pages"
    assert monitor._metric_kind("author.today") == "chapters"
    assert monitor._metric_kind("https://author.today/work/635146") == "chapters"


def test_seen_for_keeps_readli_base_recorded_with_bare_host():
    """База, записанная `_set_seen` (голый хост), сопоставима со счётом того же
    readli — иначе «на сайте больше, чем видели» истинно на каждом тике."""
    mon = Monitored(source_url=RL, last_seen_chapters=73, last_seen_source="readli.net")
    assert monitor._seen_for(mon, RL) == 73


def test_set_seen_then_seen_for_roundtrip_on_readli():
    mon = Monitored(source_url=RL, last_seen_chapters=0, last_seen_source="")
    monitor._set_seen(mon, 73, RL)
    assert monitor._seen_for(mon, RL) == 73


# --- Дефект 2: поиск readli и номер тома ---


def _card(slug: str, title: str) -> str:
    return (
        '<article class="book"><h4 class="book__title">'
        f'<a href="https://readli.net/{slug}/" title="{title}">{title}</a></h4>'
        '<div class="book__authors">'
        '<a href="/avtor/reddetonator/">"RedDetonator"</a></div></article>'
    )


def _page(*vols: int | None) -> str:
    cards = "".join(
        _card(
            f"vechno-golodnyiy-student-{v}" if v else "vechno-golodnyiy-student",
            f"{TITLE} {v}" if v else TITLE,
        )
        for v in vols
    )
    return f"<html><body>{cards}</body></html>"


# Живая выдача /srch/ на 2026-09-15: тома 2, 9, 3, 7, без номера, 6, 4 — без 10.
SEARCH = _page(2, 9, 3, 7, None, 6, 4)
# Страница автора: новые тома первыми, 10 есть.
AUTHOR = _page(10, 9, 8, None)


def _fake_site(monkeypatch, pages: dict[str, str]) -> list[str]:
    def fake_get(c, url, attempts=4):
        for key, html in pages.items():
            if key in url:
                return SimpleNamespace(status_code=200, text=html)
        return SimpleNamespace(status_code=404, text="<html></html>")

    monkeypatch.setattr(readli, "_get", fake_get)
    got: list[str] = []
    monkeypatch.setattr(readli, "download", lambda url: got.append(url) or url)
    return got


def test_search_never_returns_another_volume(monkeypatch):
    got = _fake_site(monkeypatch, {"/srch/": SEARCH})
    assert readli.search_and_download(f"{TITLE} 10", "RedDetonator") is None
    assert got == []


def test_search_picks_the_requested_volume_among_equal_titles(monkeypatch):
    _fake_site(monkeypatch, {"/srch/": SEARCH})
    assert (
        readli.search_and_download(f"{TITLE} 9", "RedDetonator")
        == "https://readli.net/vechno-golodnyiy-student-9/"
    )


def test_search_unnumbered_title_is_volume_one(monkeypatch):
    _fake_site(monkeypatch, {"/srch/": SEARCH})
    assert (
        readli.search_and_download(TITLE, "RedDetonator")
        == "https://readli.net/vechno-golodnyiy-student/"
    )


def test_search_falls_back_to_author_page_for_missing_volume(monkeypatch):
    _fake_site(monkeypatch, {"/srch/": SEARCH, "/avtor/reddetonator": AUTHOR})
    assert (
        readli.search_and_download(f"{TITLE} 10", "RedDetonator")
        == "https://readli.net/vechno-golodnyiy-student-10/"
    )


def test_author_page_fallback_needs_a_known_author(monkeypatch):
    """Без автора в запросе чужая страница автора — не доказательство: не идём."""
    _fake_site(monkeypatch, {"/srch/": SEARCH, "/avtor/reddetonator": AUTHOR})
    assert readli.search_and_download(f"{TITLE} 10", "") is None
