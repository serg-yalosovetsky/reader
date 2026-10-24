"""Ручное зеркало книги (serg/tasks#1853).

Живой случай: «Культивируй. Умри. Начни сначала.» переехала на ficbook под
новым id, старая страница отдавала 404, подписку сняли чёрным списком — и книга
перестала обновляться, а «Оригинал» вёл на мёртвый адрес.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from backend.app.db.models import Monitored, Work
from backend.app.db.session import engine

OLD = "https://ficbook.net/readfic/019fc393-fe5a-73a5-8858-c60d91fc3005"
NEW = "https://ficbook.net/readfic/01a051eb-a3c8-7a4c-b040-c0501125af2e"


_created: list[int] = []

PUBLIC_IP = "93.184.216.34"


def _resolve_to(monkeypatch, *ips: str):
    """DNS в тестах не трогаем: хост «разрешается» в заданные адреса."""
    import socket

    from backend.app import source_url

    def fake(host, port, *a, **kw):
        fam = lambda ip: socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam(ip), socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(source_url.socket, "getaddrinfo", fake)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    _resolve_to(monkeypatch, PUBLIC_IP)


@pytest.fixture(autouse=True)
def _cleanup(client):
    """База у тестов общая: книги с source_url отсюда попадали в чужие проверки
    (фоновое обновление обложек в test_manual_cover)."""
    yield
    while _created:
        client.delete(f"/api/library/{_created.pop()}")


def _work(client, body: bytes = b"mirror-book-test") -> int:
    r = client.post("/api/library/upload",
                    files={"file": ("m.epub", body, "application/epub+zip")})
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    _created.append(wid)
    with Session(engine) as s:
        w = s.get(Work, wid)
        w.source_url = OLD
        w.chapters_count = 25
        s.add(w)
        s.commit()
    return wid


def _mons(wid: int) -> list[Monitored]:
    with Session(engine) as s:
        return s.exec(select(Monitored).where(Monitored.work_id == wid)).all()


def test_primary_mirror_retargets_book_and_subscribes(client):
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW, "primary": True})
    assert r.status_code == 200, r.text
    assert r.json()["source_url"] == NEW
    mons = _mons(wid)
    assert [m.source_url for m in mons] == [NEW]
    # Стартовое «видели» = главы книги: без этого первый тик перекачал бы всё.
    assert mons[0].last_seen_chapters == 25
    assert client.get(f"/api/library/{wid}").json()["monitored"] is True


def test_extra_mirror_keeps_primary_source(client):
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW})
    assert r.status_code == 200, r.text
    assert r.json()["source_url"] == OLD


def test_mirror_lifts_blacklist(client):
    from backend.app import blacklist
    wid = _work(client)
    with Session(engine) as s:
        blacklist.add_entry(s, urls=[NEW])
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW, "primary": True})
    assert r.status_code == 200, r.text
    with Session(engine) as s:
        assert not blacklist.is_blacklisted(s, source_url=NEW)


def test_mirror_rejects_non_url_and_foreign_book(client):
    wid = _work(client)
    assert client.post(f"/api/library/{wid}/mirror", json={"url": "нет"}).status_code == 400
    other = _work(client, b"another-mirror-book")
    url = "https://ficbook.net/readfic/01a0ffff-0000-7000-8000-000000000001"
    assert client.post(f"/api/library/{other}/mirror", json={"url": url}).status_code == 200
    assert client.post(f"/api/library/{wid}/mirror", json={"url": url}).status_code == 409


# --- serg/tasks#1859: SSRF — зеркало только на сайты-источники и глобальные IP ---


@pytest.mark.parametrize("url, reason", [
    ("https://example.com/book/1", "не поддерживается"),
    ("https://evilficbook.net/readfic/1", "не поддерживается"),
    ("https://ficbook.net.evil.com/readfic/1", "не поддерживается"),
    ("http://127.0.0.1/api/library", "IP-адрес"),
    ("http://127.0.0.1:8000/api/library", "порт"),
    ("http://169.254.169.254/latest/meta-data/", "IP-адрес"),
    ("http://[::1]/", "IP-адрес"),
    ("http://localhost/", "не поддерживается"),
    ("ftp://ficbook.net/readfic/1", "http/https"),
    ("file:///etc/passwd", "http/https"),
    ("https://user:pw@ficbook.net/readfic/1", "логина"),
    ("https://ficbook.net:8080/readfic/1", "порт"),
])
def test_mirror_rejects_unsupported_or_internal(client, url, reason):
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": url})
    assert r.status_code == 400, r.text
    assert reason in r.json()["detail"]
    assert _mons(wid) == []


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254",
                                "100.66.108.118", "::1", "fe80::1", "::ffff:127.0.0.1"])
def test_mirror_rejects_supported_host_resolving_inside(client, monkeypatch, ip):
    """DNS ficbook.net подменён (или rebinding) — внутрь всё равно не пускаем."""
    _resolve_to(monkeypatch, PUBLIC_IP, ip)
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW})
    assert r.status_code == 400, r.text
    assert "внутренний адрес" in r.json()["detail"]
    assert _mons(wid) == []


def test_mirror_unresolvable_host_is_400(client, monkeypatch):
    import socket

    from backend.app import source_url

    def boom(*a, **kw):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(source_url.socket, "getaddrinfo", boom)
    wid = _work(client)
    r = client.post(f"/api/library/{wid}/mirror", json={"url": NEW})
    assert r.status_code == 400
    assert "не удалось найти адрес" in r.json()["detail"]


@pytest.mark.parametrize("url", [
    "https://author.today/work/12345",
    "https://www.fanfiction.net/s/1/1/",
    "https://m.fanfiction.net/s/1/1/",
    "https://archiveofourown.org/works/1",
    "https://fanfics.me/fic1",
    "https://readli.net/book/1/",
    "https://searchfloor.org/book/1",
    "https://FICBOOK.NET./readfic/abc",
])
def test_supported_sources_pass_validator(url):
    from backend.app.source_url import validate_source_url

    assert validate_source_url("  " + url + " ") == url
