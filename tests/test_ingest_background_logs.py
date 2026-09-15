"""spec.reader.update-pipeline v12 — добавление ссылки не должно упираться в nginx,
а начало и итог каждого скачивания обязаны быть видны в логах (Loki/Grafana).

Живой случай 2026-09-15 (serg/tasks#887, #888): «Червь» (ficbook, 311 глав)
качался ~7 минут. `POST /api/ingest` был синхронным, nginx оборвал его через
300 с — пользователь увидел «Не удалось добавить: <html>504 Gateway Time-out»,
хотя книга через 2,5 минуты честно появилась в библиотеке. В Loki за всё это
время у service=reader не было ни одной строки: скачивание не логировало ни
старт, ни итог, ни провал.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from sqlmodel import Session

from backend.accounts import monitor
from backend.app.db.models import Monitored, Work
from backend.app.routers import ingest as ingest_router
from backend.downloaders.base import DownloaderError, DownloadResult

URL = "https://ficbook.net/readfic/3658527"


class _Grab(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


@pytest.fixture
def logs():
    """Хендлер прямо на логгерах приложения: у «reader» propagate=False после
    старта lifespan, и caplog (он висит на root) их не видит."""
    grab = _Grab()
    targets = [logging.getLogger("reader.ingest"), logging.getLogger("reader.monitor")]
    saved = [(lg, lg.level) for lg in targets]
    for lg in targets:
        lg.addHandler(grab)
        lg.setLevel(logging.INFO)
    yield grab
    for lg, level in saved:
        lg.removeHandler(grab)
        lg.setLevel(level)


def _result() -> DownloadResult:
    return DownloadResult(
        file_path=Path("/tmp/book.epub"),
        file_format="epub",
        title="Червь",
        author="Wildbow",
        site="ficbook",
        source_url=URL,
        num_chapters=311,
    )


def _work(**kw) -> Work:
    kw.setdefault("id", 3693)
    return Work(
        title="Червь",
        author="Wildbow",
        site="ficbook",
        source_url=URL,
        file_path="/tmp/book.epub",
        file_format="epub",
        sha1="deadbeef",
        chapters_count=311,
        **kw,
    )


def _patch_ok(monkeypatch):
    monkeypatch.setattr(ingest_router.chain, "fetch", lambda q, creds=None: _result())
    monkeypatch.setattr(ingest_router, "register_download", lambda res, session: _work())
    monkeypatch.setattr(ingest_router.monitor, "add_monitor", lambda *a, **k: None)


def _patch_fail(monkeypatch):
    def boom(q, creds=None):
        raise DownloaderError("403 Client Error: Forbidden")

    monkeypatch.setattr(ingest_router.chain, "fetch", boom)


def _wait_job(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get(f"/api/ingest/jobs/{job_id}").json()
        if st["status"] in ("done", "error"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"задание {job_id} не завершилось за {timeout} с")


# --- Логи скачивания при добавлении ссылки ---


def test_sync_ingest_logs_start_and_result(client, monkeypatch, logs):
    _patch_ok(monkeypatch)
    r = client.post("/api/ingest", json={"query": URL})
    assert r.status_code == 200
    text = "\n".join(logs.lines)
    assert "скачивание начато" in text and URL in text
    assert "скачано" in text and "Червь" in text and "311" in text


def test_sync_ingest_failure_is_logged_with_reason(client, monkeypatch, logs):
    _patch_fail(monkeypatch)
    r = client.post("/api/ingest", json={"query": URL})
    assert r.status_code == 422
    warn = [ln for ln in logs.lines if ln.startswith("WARNING")]
    assert any("не удалось" in ln and "403" in ln and URL in ln for ln in warn)


# --- Фоновое добавление: ответ сразу, статус опросом ---


def test_background_ingest_answers_immediately_and_finishes(client, monkeypatch, logs):
    _patch_ok(monkeypatch)
    r = client.post("/api/ingest", json={"query": URL, "background": True})
    assert r.status_code == 202
    job = r.json()
    assert job["status"] in ("queued", "running", "started")
    st = _wait_job(client, job["job_id"])
    assert st["status"] == "done", st
    assert st["work"]["id"] == 3693
    assert st["work"]["title"] == "Червь"
    assert st["work"]["chapters"] == 311
    assert st["error"] is None
    assert st["elapsed_s"] is not None
    assert any("скачивание начато" in ln for ln in logs.lines)


def test_background_ingest_error_is_a_status_not_silence(client, monkeypatch, logs):
    _patch_fail(monkeypatch)
    r = client.post("/api/ingest", json={"query": URL, "background": True})
    assert r.status_code == 202
    st = _wait_job(client, r.json()["job_id"])
    assert st["status"] == "error"
    assert "403" in st["error"]
    assert any(ln.startswith("WARNING") and "403" in ln for ln in logs.lines)


def test_background_empty_query_rejected_upfront(client):
    r = client.post("/api/ingest", json={"query": "   ", "background": True})
    assert r.status_code == 400


def test_unknown_job_is_404(client):
    assert client.get("/api/ingest/jobs/nope").status_code == 404


# --- Логи докачки монитором (автообновление) ---


def test_monitor_download_logs_start_and_result(session: Session, monkeypatch, logs):
    w = _work(id=None)
    session.add(w)
    session.commit()
    session.refresh(w)
    mon = Monitored(source_url=URL, work_id=w.id, last_seen_chapters=310,
                    last_seen_source="ficbook.net")
    session.add(mon)
    session.commit()
    session.refresh(mon)

    monkeypatch.setattr(monitor, "_fetch_at_cover_bytes", lambda *a: None)
    monkeypatch.setattr(monitor, "_apply_at_cover", lambda *a: None)
    monkeypatch.setattr(monitor.chain, "fetch_fullest", lambda *a, **k: _result())
    monkeypatch.setattr(monitor, "register_download", lambda res, s: s.get(Work, w.id))
    from backend.app import services

    monkeypatch.setattr(services, "count_sections", lambda *a, **k: 311)

    monitor._download_and_write(session, mon, w, URL, 311)
    text = "\n".join(logs.lines)
    assert "докачка начата" in text and URL in text and "Червь" in text
    assert "докачка завершена" in text and "311" in text


def test_monitor_download_failure_is_logged(session: Session, monkeypatch, logs):
    w = _work(id=None)
    session.add(w)
    session.commit()
    session.refresh(w)
    mon = Monitored(source_url=URL, work_id=w.id)
    session.add(mon)
    session.commit()
    session.refresh(mon)

    def boom(*a, **k):
        raise DownloaderError("FanFicFare превысил тайм-аут")

    monkeypatch.setattr(monitor, "_fetch_at_cover_bytes", lambda *a: None)
    monkeypatch.setattr(monitor.chain, "fetch_fullest", lambda *a, **k: None)
    monkeypatch.setattr(monitor.chain, "fetch", boom)
    with pytest.raises(DownloaderError):
        monitor._download_and_write(session, mon, w, URL, 311)
    assert any(
        ln.startswith("WARNING") and "докачка не удалась" in ln and "тайм-аут" in ln
        for ln in logs.lines
    )
