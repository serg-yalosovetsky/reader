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
from backend.downloaders.redirect_guard import (
    RedirectBlocked,
    UrlBlocked,
    check_redirect_target,
    check_url,
)

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
    "http://127.0.0.1:8443/",
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


def test_article_endpoint_refuses_ssrf(client):
    """GET /api/article?url= отвергает внутренние адреса с кодом 400 и причиной."""
    r1 = client.get("/api/article?url=http://127.0.0.1:8000/")
    assert r1.status_code == 400
    assert "запрещённый адрес" in r1.json()["detail"] or "порт" in r1.json()["detail"]

    r2 = client.get("/api/article?url=http://169.254.169.254/")
    assert r2.status_code == 400
    assert "169.254.169.254" in r2.json()["detail"] or "внутренний адрес" in r2.json()["detail"]


def test_article_endpoint_refuses_redirect_to_internal(client, monkeypatch):
    """GET /api/article?url= перехватывает редирект во внутреннюю сеть и отдаёт 400."""
    def handler(req):
        if req.url.host == "127.0.0.1":
            return httpx.Response(200, text="secret")
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:80/"})

    real_async = httpx.AsyncClient

    def make_async(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_async(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", make_async)
    r = client.get("/api/article?url=https://example.com/art")
    assert r.status_code == 400
    assert "запрещённый адрес" in r.json()["detail"] or "127.0.0.1" in r.json()["detail"]


def test_searchfloor_subdomain_bypass():
    """evilsearchfloor.org не должен распознаваться как searchfloor.org."""
    from backend.downloaders import searchfloor

    assert not searchfloor.supports("http://evilsearchfloor.org/b/123")
    assert not searchfloor.supports("https://evilsearchfloor.org/book/123")
    assert not searchfloor.supports("http://notsearchfloor.org/post/1")
    assert searchfloor.supports("https://searchfloor.org/book/123")
    assert searchfloor.supports("https://sub.searchfloor.org/b/123")
    assert searchfloor.supports("http://searchfloor.org/post/100")


def test_chain_does_not_route_evilsearchfloor_to_searchfloor(monkeypatch):
    """Цепочка chain не отдаёт evilsearchfloor.org адаптеру searchfloor."""
    from backend.downloaders import chain, searchfloor

    called = []
    monkeypatch.setattr(searchfloor, "download", lambda u: called.append(u))

    with pytest.raises(DownloaderError):
        chain.fetch("https://evilsearchfloor.org/b/123")
    assert not called, "searchfloor.download не должен вызываться для чужого домена"


@pytest.mark.parametrize("url,match", [
    ("http://127.0.0.1:8000/", "127.0.0.1"),
    ("http://169.254.169.254/", "169.254.169.254"),
    ("http://[::1]/", "::1"),
    ("http://user:pw@example.com/", "логина и пароля"),
    ("ftp://example.com/", "только http/https"),
])
def test_check_url_blocked(url, match):
    with pytest.raises(UrlBlocked, match=match):
        check_url(url)


def test_article_endpoint_success_on_allowed_url(client, monkeypatch):
    """GET /api/article на разрешённый адрес отдаёт 200 с текстом статьи (не 502)."""
    article_html = (
        "<html><head><title>Test Article</title></head><body>"
        "<article><h1>Test Article</h1>"
        + "<p>Paragraph of text to ensure length is over 200 characters. " * 5
        + "</p></article></body></html>"
    )

    def handler(req):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=article_html,
        )

    real_async = httpx.AsyncClient

    def make_async(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_async(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", make_async)
    r = client.get("/api/article?url=https://example.com/art")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Test Article"
    assert "Paragraph of text" in data["text"]


def test_requests_redirect_hook_allows_legal_302():
    """requests_redirect_hook возвращает None и не ломает законный 302 редирект."""
    import requests
    from backend.downloaders.redirect_guard import requests_redirect_hook

    class MockAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            if "redirected" in request.url:
                resp = requests.Response()
                resp.status_code = 200
                resp.url = request.url
                resp.request = request
                resp._content = b"success"
                return resp
            resp = requests.Response()
            resp.status_code = 302
            resp.url = request.url
            resp.request = request
            resp.headers["Location"] = "https://example.com/redirected"
            return resp

    session = requests.Session()
    session.mount("https://", MockAdapter())
    session.hooks["response"].append(requests_redirect_hook)
    r = session.get("https://example.com/start")
    assert r.status_code == 200
    assert r.text == "success"
    assert isinstance(r, requests.Response)


def test_requests_redirect_hook_blocks_internal_302():
    """requests_redirect_hook перехватывает редирект на loopback."""
    import requests
    from backend.downloaders.redirect_guard import requests_redirect_hook

    class MockAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.status_code = 302
            resp.url = request.url
            resp.request = request
            resp.headers["Location"] = "http://127.0.0.1/admin"
            return resp

    session = requests.Session()
    session.mount("https://", MockAdapter())
    session.hooks["response"].append(requests_redirect_hook)
    with pytest.raises(RedirectBlocked):
        session.get("https://example.com/start")


def test_host_matches_and_evil_ficbook():
    """evil-ficbook.net не должен матчиться на ficbook.net."""
    from backend.downloaders.fanficfare_engine import _needs_cloudscraper
    from backend.downloaders.redirect_guard import host_matches

    assert not host_matches("evil-ficbook.net", "ficbook.net")
    assert not host_matches("notficbook.net", "ficbook.net")
    assert not host_matches("evilficbook.net", "ficbook.net")
    assert host_matches("ficbook.net", "ficbook.net")
    assert host_matches("sub.ficbook.net", "ficbook.net")
    assert host_matches("sub.sub.ficbook.net", "ficbook.net")

    assert not _needs_cloudscraper("https://evil-ficbook.net/readfic/1")
    assert not _needs_cloudscraper("https://notficbook.net/readfic/1")
    assert _needs_cloudscraper("https://ficbook.net/readfic/1")
    assert _needs_cloudscraper("https://sub.ficbook.net/readfic/1")


def test_check_url_allows_nonstandard_ports():
    """Порты 8080/8443 и другие на глобальных IP разрешены."""
    u = check_url("https://example.com:8443/test")
    assert u.port == 8443
    assert str(u) == "https://example.com:8443/test"

    u2 = check_url("http://example.com:8080/")
    assert u2.port == 8080


def test_check_url_unresolvable_raises_subclass(monkeypatch):
    """Неразрешимый хост бросает UrlUnresolvable (наследник UrlBlocked)."""
    from backend.downloaders.redirect_guard import UrlUnresolvable

    def boom(*a, **kw):
        raise socket.gaierror("lookup failure")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(UrlUnresolvable) as exc_info:
        check_url("https://unknown-host-does-not-exist.org/")
    assert issubclass(UrlUnresolvable, UrlBlocked)
    assert "lookup failure" in exc_info.value.reason

