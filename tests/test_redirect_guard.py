"""serg/tasks#2330: загрузчики не идут по редиректу во внутреннюю сеть.

Поддерживаемый хост отвечает 302 на http://127.0.0.1/ — загрузка падает с
понятным отказом, а запрос к 127.0.0.1 не уходит вовсе.
"""

from __future__ import annotations

import logging
import socket

import httpx
import pytest

from backend.downloaders import egress, fichub, pythondocs, readli
from backend.downloaders.base import DownloaderError
from backend.downloaders.redirect_guard import RedirectBlocked, check_redirect_target

_PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    """Имена — в публичный адрес, IP-литералы — как есть; в сеть не ходим."""
    real = socket.getaddrinfo

    def fake(host, port, *a, **kw):
        if host[0].isdigit() or ":" in host:
            return real(host, port, *a, **kw)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(readli.time, "sleep", lambda *_: None)


def _patch_client(monkeypatch, handler):
    real = httpx.Client

    def make(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "Client", make)


def _redirect_to_localhost(seen):
    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "127.0.0.1":
            return httpx.Response(200, text="internal secret")
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})
    return handler


def test_readli_redirect_to_localhost_refused(monkeypatch, caplog):
    seen: list[str] = []
    _patch_client(monkeypatch, _redirect_to_localhost(seen))
    with (caplog.at_level(logging.WARNING, logger="reader.redirect_guard"),
          pytest.raises(RedirectBlocked, match="127.0.0.1")):
        readli.download("https://readli.net/chitat-online/?b=1")
    assert all("127.0.0.1" not in u for u in seen), seen
    assert seen and len(seen) == 1, "отказ не должен ретраиться как сетевой сбой"
    assert any("редирект отклонён" in r.message for r in caplog.records)


def test_authortoday_client_refuses_redirect(monkeypatch):
    seen: list[str] = []
    _patch_client(monkeypatch, _redirect_to_localhost(seen))
    with egress.at_client(follow_redirects=True) as c, pytest.raises(RedirectBlocked):
        c.get("https://author.today/work/1")
    assert seen == ["https://author.today/work/1"]


def test_fichub_redirect_refused(monkeypatch):
    seen: list[str] = []
    _patch_client(monkeypatch, _redirect_to_localhost(seen))
    with pytest.raises(DownloaderError):
        fichub.download("https://archiveofourown.org/works/1")
    assert all("127.0.0.1" not in u for u in seen), seen


def test_pythondocs_redirect_refused(monkeypatch):
    seen: list[str] = []
    _patch_client(monkeypatch, _redirect_to_localhost(seen))
    with pytest.raises(DownloaderError):
        pythondocs.current_version()
    assert all("127.0.0.1" not in u for u in seen), seen


def test_public_redirect_still_followed(monkeypatch):
    def handler(req):
        if req.url.host == "cdn.example.com":
            return httpx.Response(200, text="ok")
        return httpx.Response(302, headers={"Location": "https://cdn.example.com/f"})

    _patch_client(monkeypatch, handler)
    with egress.at_client(follow_redirects=True) as c:
        assert c.get("https://author.today/x").text == "ok"


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/",
    "http://[::1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.66.108.118/",
    "https://author.today:8443/",
    "ftp://author.today/",
    "http://user:pw@author.today/",
])
def test_bad_targets(location):
    with pytest.raises(RedirectBlocked):
        check_redirect_target(httpx.URL("https://author.today/a"), location)


def test_name_resolving_to_private_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC, p)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", p)),
    ])
    with pytest.raises(RedirectBlocked, match="10.0.0.5"):
        check_redirect_target(httpx.URL("https://author.today/a"), "https://evil.example/")


def test_unresolvable_target_refused(monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(RedirectBlocked, match="разрешить"):
        check_redirect_target(httpx.URL("https://author.today/a"), "/relative")


def test_cover_og_image_to_internal_address_not_fetched(monkeypatch):
    """og:image из HTML источника указывает во внутреннюю сеть — запроса туда нет."""
    from backend.app import covers

    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "127.0.0.1":
            return httpx.Response(200, content=b"x" * 1000)
        return httpx.Response(
            200, text='<meta property="og:image" content="http://127.0.0.1/admin.png">'
        )

    _patch_client(monkeypatch, handler)
    assert covers.fetch_cover_bytes("https://author.today/work/1") is None
    assert not any("127.0.0.1" in u for u in seen)
