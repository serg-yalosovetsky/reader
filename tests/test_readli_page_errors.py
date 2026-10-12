"""readli: 503 посреди книги не должен молча отрезать последние главы.

Живой случай — «Вечно голодный студент»: обновление получало 503, а хвост книги
пропадал. `download()` не проверял код ответа у страниц 2..N, и заглушка 503
давала пустой текст — книга собиралась без последних глав без единой ошибки.
"""

from __future__ import annotations

import httpx
import pytest

from backend.downloaders import readli
from backend.downloaders.base import DownloaderError


def _page(n: int, total: int) -> str:
    return (
        f"<html><head><title>{n}/{total} Книга | Ридли</title></head><body>"
        f'<div class="reading__text"><h3>Глава {n}</h3><p>текст {n}</p></div>'
        "</body></html>"
    )


def _client(handler):
    real = httpx.Client

    def make(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    return make


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(readli.time, "sleep", lambda *_: None)
    monkeypatch.setattr(readli, "build_epub", lambda *a, **kw: __import__("pathlib").Path("/tmp/x.epub"))
    monkeypatch.setattr(readli.progress, "report", lambda *a, **kw: None)


def test_transient_503_is_retried(monkeypatch):
    hits: dict[int, int] = {}

    def handler(req):
        n = int(req.url.params["pg"])
        hits[n] = hits.get(n, 0) + 1
        if n == 3 and hits[n] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=_page(n, 3))

    monkeypatch.setattr(readli.httpx, "Client", _client(handler))
    res = readli.download("https://readli.net/chitat-online/?b=1")
    assert res.num_chapters == 3
    assert hits[3] == 3


def test_persistent_503_fails_instead_of_dropping_tail(monkeypatch):
    def handler(req):
        n = int(req.url.params["pg"])
        if n == 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text=_page(n, 3))

    monkeypatch.setattr(readli.httpx, "Client", _client(handler))
    with pytest.raises(DownloaderError, match="3/3 вернула 503"):
        readli.download("https://readli.net/chitat-online/?b=1")


def test_503_message_is_retryable_for_ingest():
    from backend.app.ingestjob import _is_retryable

    assert _is_retryable("readli: страница 3/3 вернула 503")
