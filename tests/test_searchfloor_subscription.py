"""searchfloor: книга «по подписке» — не зеркало и не временный 401 (serg/tasks#1858).

Живой случай 14–19.10.2026: author.today лежал (домашний egress offline), монитор
взял счётчик зеркала searchfloor — плашка «Последняя глава» говорила 23 — для
«Крушение сурка. Том 3» (work 2885). Книга на searchfloor была закрыта замком
«Доступ по подписке», /book/<id> анониму отвечал 401, докачка приносила 22 главы,
и подписка пять тиков подряд штрафовалась «докачано 22 гл., на сайте 23 — глава
недоступна?», пока не выпала в backoff.

Фикстуры — реальная разметка searchfloor.org от 24.10.2026:
  b23199_subscription.html — онгоинг с замком (/book/23199 → 401);
  b26765_krushenie_surka_3_free.html — та же книга, что в инциденте, уже без замка
  (/book/26765 → 200, 25 глав).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend.accounts import monitor
from backend.downloaders import searchfloor
from backend.downloaders.base import DownloaderError

FIX = Path(__file__).parent / "fixtures" / "searchfloor"
GATED = (FIX / "b23199_subscription.html").read_text(encoding="utf-8")
FREE = (FIX / "b26765_krushenie_surka_3_free.html").read_text(encoding="utf-8")


def test_lock_marker_detected_on_real_markup():
    assert searchfloor.is_subscription_only(GATED) is True
    assert searchfloor.is_subscription_only(FREE) is False
    assert searchfloor.is_subscription_only("") is False


def _mock_sf(monkeypatch, page: str, book_codes: list[int]):
    seq = iter(book_codes)
    book_calls: list[int] = []

    def handler(req):
        if "/book/" in req.url.path:
            c = next(seq)
            book_calls.append(c)
            return httpx.Response(c, content=b"PKfake" if c == 200 else b"")
        return httpx.Response(200, text=page)

    real = httpx.Client
    monkeypatch.setattr(searchfloor.httpx, "Client",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(searchfloor.time, "sleep", lambda *_: None)
    monkeypatch.setattr(searchfloor, "_book_meta", lambda bid: ("Безумный Механик том 4", "Кай Ханси"))
    return book_calls


def test_subscription_401_named_and_not_retried(monkeypatch):
    calls = _mock_sf(monkeypatch, GATED, [401, 401, 401])
    with pytest.raises(searchfloor.SubscriptionRequired, match="только подписчикам"):
        searchfloor._download_book("23199", "https://searchfloor.org/b/23199")
    assert calls == [401]  # без пустых повторов 5 с + 20 с
    # Остаётся DownloaderError: chain._search_free/fetch_fullest ловят именно его.
    assert issubclass(searchfloor.SubscriptionRequired, DownloaderError)


def test_free_page_401_still_retried_as_transient(monkeypatch):
    calls = _mock_sf(monkeypatch, FREE, [401, 200])
    res = searchfloor._download_book("26765", "https://searchfloor.org/b/26765")
    assert res.file_path.exists()
    assert calls == [401, 200]


def test_unreachable_page_is_unknown_not_free(monkeypatch, caplog):
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    real = httpx.Client
    monkeypatch.setattr(searchfloor.httpx, "Client",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    with caplog.at_level("WARNING", logger="reader.searchfloor"):
        assert searchfloor.subscription_only("1") is None
    assert "проверки подписки" in caplog.text


def _stub_probe(monkeypatch, page: str, chapters: int):
    monkeypatch.setattr(searchfloor, "search_book", lambda t, a="": "26765")
    monkeypatch.setattr(searchfloor, "_book_meta", lambda bid: ("Крушение сурка. Том 3", "noslnosl"))
    monkeypatch.setattr(searchfloor, "count_chapters", lambda url: chapters)
    monkeypatch.setattr(searchfloor, "subscription_only",
                        lambda bid: searchfloor.is_subscription_only(page))


OUR = {"title": "Крушение сурка. Том 3", "author": "noslnosl", "annotation": ""}


def test_gated_searchfloor_is_not_a_mirror(monkeypatch):
    """Сценарий инцидента: замок на странице → зеркала нет, счётчик 23 не берётся."""
    _stub_probe(monkeypatch, GATED, 23)
    assert monitor._check_searchfloor_source(OUR) is None


def test_free_searchfloor_is_still_a_mirror(monkeypatch):
    _stub_probe(monkeypatch, FREE, 23)
    assert monitor._check_searchfloor_source(OUR) == ("https://searchfloor.org/b/26765", 23)
