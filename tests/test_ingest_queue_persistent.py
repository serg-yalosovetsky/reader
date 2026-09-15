"""spec.reader 2a v7 — задания скачивания переживают рестарт (serg/tasks#892).

Живой случай 2026-09-15: задания POST /api/ingest {background:true} жили в памяти
процесса. `systemctl restart reader` убивал поток и подпроцесс FanFicFare, задание
пропадало без следа: опрос получал 404, в логе не было даже строки об обрыве.

Разбор architect: очередь — таблица `ingest_job`; «свои» прерванные задания
определяются по worker_unit (dev-копия ридера на той же БД не должна
перехватывать задания прода); плановая остановка возвращает задание в очередь
без штрафа, крэш — со штрафом; после трёх крэшей подряд задание не
возобновляется (защита от карусели OOM).
"""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from backend.app import ingest_service, ingestjob, storage
from backend.app.db.models import IngestJob, Work
from backend.app.db.session import engine, init_db
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
    grab = _Grab()
    lg = logging.getLogger("reader.ingest")
    level = lg.level
    lg.addHandler(grab)
    lg.setLevel(logging.INFO)
    yield grab
    lg.removeHandler(grab)
    lg.setLevel(level)


@pytest.fixture
def queue_db():
    """Таблица заданий пуста; воркеры НЕ запущены — задания крутим вручную."""
    init_db()
    ingestjob.stop_workers(mark=False)
    with Session(engine) as s:
        for row in s.exec(select(IngestJob)).all():
            s.delete(row)
        s.commit()
    yield


def _row(job_id: str) -> IngestJob:
    with Session(engine) as s:
        row = s.get(IngestJob, job_id)
        assert row is not None
        s.expunge(row)
        return row


def _put(**kw) -> str:
    job = IngestJob(**kw)
    with Session(engine) as s:
        s.add(job)
        s.commit()
        return job.id


def _result() -> DownloadResult:
    return DownloadResult(
        file_path=Path("/tmp/book.epub"), file_format="epub", title="Червь",
        author="SadSonya", site="ficbook", source_url=URL, num_chapters=311,
    )


def _patch_ok(monkeypatch, calls: list | None = None):
    def fetch(q, creds=None):
        if calls is not None:
            calls.append(q)
        return _result()

    monkeypatch.setattr(ingest_service.chain, "fetch", fetch)
    monkeypatch.setattr(
        ingest_service, "register_download",
        lambda res, session: Work(id=3693, title="Червь", author="SadSonya",
                                  site="ficbook", source_url=URL, chapters_count=311),
    )
    monkeypatch.setattr(ingest_service.monitor, "add_monitor", lambda *a, **k: None)


# --- Задание живёт в БД, а не в памяти процесса ---


def test_enqueue_writes_a_row_that_outlives_the_process(queue_db):
    snap = ingestjob.enqueue(URL)
    assert snap["status"] == "queued"
    row = _row(snap["job_id"])
    assert row.status == "queued" and row.query == URL
    # «Рестарт»: в памяти ничего нет, статус всё равно отдаётся из БД.
    assert ingestjob.get(snap["job_id"])["job_id"] == snap["job_id"]


def test_worker_runs_job_to_done_with_work(queue_db, monkeypatch, logs):
    _patch_ok(monkeypatch)
    job_id = ingestjob.enqueue(URL)["job_id"]
    assert ingestjob.process_one() is True
    st = ingestjob.get(job_id)
    assert st["status"] == "done", st
    assert st["work"] == {"id": 3693, "title": "Червь", "author": "SadSonya", "chapters": 311}
    assert st["attempts"] == 1
    assert any("скачано" in ln for ln in logs.lines)


def test_download_error_becomes_error_status(queue_db, monkeypatch):
    def boom(q, creds=None):
        raise DownloaderError("Story does not exist")

    monkeypatch.setattr(ingest_service.chain, "fetch", boom)
    job_id = ingestjob.enqueue(URL)["job_id"]
    assert ingestjob.process_one() is True
    st = ingestjob.get(job_id)
    assert st["status"] == "error" and "Story does not exist" in st["error"]


# --- Рестарт: плановая остановка и крэш ---


def test_shutdown_requeues_own_running_job_without_burning_an_attempt(queue_db):
    job_id = _put(query=URL, status="running", attempts=1,
                  worker_unit=ingestjob.WORKER_UNIT, worker_boot=ingestjob.WORKER_BOOT)
    ingestjob.mark_shutdown()
    row = _row(job_id)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.interrupted_by == "shutdown"
    assert row.interruptions == 1


def test_startup_resumes_own_job_interrupted_by_crash(queue_db, monkeypatch, logs):
    job_id = _put(query=URL, status="running", attempts=1,
                  worker_unit=ingestjob.WORKER_UNIT, worker_boot="previous-boot")
    ingestjob.recover_on_startup()
    row = _row(job_id)
    assert row.status == "queued"
    assert row.interrupted_by == "crash"
    assert row.attempts == 1  # крэш — штраф остаётся
    assert row.not_before is not None and row.not_before > ingestjob.now()

    # Отсрочку снимаем — задание доходит до done и в логе видно возобновление.
    with Session(engine) as s:
        r = s.get(IngestJob, job_id)
        r.not_before = ingestjob.now() - timedelta(seconds=1)
        s.add(r)
        s.commit()
    _patch_ok(monkeypatch)
    assert ingestjob.process_one() is True
    assert ingestjob.get(job_id)["status"] == "done"
    assert any("возобновл" in ln and job_id in ln for ln in logs.lines)


def test_startup_does_not_touch_other_workers_jobs(queue_db):
    job_id = _put(query=URL, status="running", attempts=1,
                  worker_unit="reader.service@other-host", worker_boot="x")
    ingestjob.recover_on_startup()
    assert _row(job_id).status == "running"


def test_three_crashes_stop_the_carousel(queue_db, monkeypatch):
    calls: list = []
    _patch_ok(monkeypatch, calls)
    job_id = _put(query=URL, status="queued", attempts=3, max_attempts=3,
                  interruptions=3, interrupted_by="crash")
    assert ingestjob.process_one() is True
    st = ingestjob.get(job_id)
    assert st["status"] == "error"
    assert "прервано" in st["error"]
    assert calls == []  # не качали снова


def test_not_before_is_respected(queue_db, monkeypatch):
    calls: list = []
    _patch_ok(monkeypatch, calls)
    job_id = _put(query=URL, status="queued",
                  not_before=ingestjob.now() + timedelta(minutes=5))
    assert ingestjob.process_one() is False
    assert _row(job_id).status == "queued" and calls == []


# --- Повторная отправка и лимит очереди ---


def test_same_active_query_is_deduplicated(queue_db, monkeypatch):
    first = ingestjob.enqueue(URL)
    second = ingestjob.enqueue(URL)
    assert second["job_id"] == first["job_id"]
    assert second["deduplicated"] is True
    _patch_ok(monkeypatch)
    ingestjob.process_one()
    third = ingestjob.enqueue(URL)  # прошлое завершилось — это новое задание
    assert third["job_id"] != first["job_id"]


def test_queue_limit(queue_db, monkeypatch):
    monkeypatch.setattr(ingestjob, "MAX_ACTIVE", 2)
    ingestjob.enqueue(URL + "?a")
    ingestjob.enqueue(URL + "?b")
    with pytest.raises(ingestjob.QueueFull):
        ingestjob.enqueue(URL + "?c")


def test_api_queue_full_is_429(client, monkeypatch):
    def full(q, kind="ingest"):
        raise ingestjob.QueueFull("очередь переполнена")

    monkeypatch.setattr(ingestjob, "enqueue", full)
    r = client.post("/api/ingest", json={"query": URL, "background": True})
    assert r.status_code == 429


# --- Секреты в логах и целостность файла книги ---


def test_userinfo_is_masked_in_logs(queue_db, monkeypatch, logs):
    def boom(q, creds=None):
        raise DownloaderError("403")

    monkeypatch.setattr(ingest_service.chain, "fetch", boom)
    ingestjob.enqueue("https://user:s3cr3t@ficbook.net/readfic/1")
    ingestjob.process_one()
    assert logs.lines
    assert not any("s3cr3t" in ln for ln in logs.lines)


def test_import_file_leaves_no_half_written_book(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "BOOKS_DIR", tmp_path / "books")
    src = tmp_path / "book.epub"
    src.write_bytes(b"x" * 4096)

    def broken_copy(a, b, *args, **kw):
        Path(b).write_bytes(b"x" * 100)  # недописанный файл
        raise OSError("диск закончился")

    monkeypatch.setattr(shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        storage.import_file(src, "deadbeef")
    books = tmp_path / "books"
    assert not (books / "deadbeef.epub").exists()
    assert not any(books.iterdir()) if books.exists() else True
