"""Трассировка упавшего задания не выносит в общий Loki логин/пароль из URL.

Возврат приёмки serg/tasks#892 (security-reviewer, HIGH): process_one ловил
неожиданное исключение через log.exception. Сообщение маскировалось, а traceback
с сырым str(e) логгер дописывал сам — ссылка вида https://user:pass@host уходила
в общий для меша Loki немаскированной. Прежний тест смотрел только
record.getMessage() и трассировку не видел вовсе — поэтому хендлер здесь
форматирует запись ЦЕЛИКОМ, вместе с traceback.
"""

from __future__ import annotations

import logging

import pytest
from sqlmodel import Session, select

from backend.app import ingest_service, ingestjob
from backend.app.db.models import IngestJob
from backend.app.db.session import engine, init_db

SECRET_URL = "https://user:s3cr3t@ficbook.net/readfic/1"


class _Formatted(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.text.append(self.format(record))  # включает traceback, если он приложен


@pytest.fixture
def logs():
    h = _Formatted()
    lg = logging.getLogger("reader.ingest")
    level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    yield h
    lg.removeHandler(h)
    lg.setLevel(level)


@pytest.fixture
def queue_db():
    init_db()
    ingestjob.stop_workers(mark=False)
    with Session(engine) as s:
        for row in s.exec(select(IngestJob)).all():
            s.delete(row)
        s.commit()
    yield


def test_unexpected_exception_traceback_is_masked(queue_db, monkeypatch, logs):
    def boom(q, creds=None):
        raise RuntimeError(f"connection failed for {SECRET_URL}")

    monkeypatch.setattr(ingest_service.chain, "fetch", boom)
    job_id = ingestjob.enqueue(SECRET_URL)["job_id"]
    assert ingestjob.process_one() is True

    full = "\n".join(logs.text)
    assert "RuntimeError" in full  # причина видна
    assert "Traceback" in full  # трассировка сохранена — по ней чинят
    assert "s3cr3t" not in full

    st = ingestjob.get(job_id)
    assert st["status"] == "error"
    assert "s3cr3t" not in (st["error"] or "")
    assert "s3cr3t" not in st["query"]


def test_worker_loop_failure_traceback_is_masked(monkeypatch, logs):
    def broken():
        raise RuntimeError(f"db said: duplicate key for {SECRET_URL}")

    monkeypatch.setattr(ingestjob, "process_one", broken)
    monkeypatch.setattr(ingestjob, "POLL_S", 0.01)
    gen = ingestjob._generation

    calls = {"n": 0}
    real_wait = ingestjob._wake.wait

    def wait_once(timeout=None):
        calls["n"] += 1
        ingestjob._stop.set()  # один оборот цикла и выход
        return real_wait(0)

    monkeypatch.setattr(ingestjob._wake, "wait", wait_once)
    ingestjob._stop.clear()
    try:
        ingestjob._loop(gen)
    finally:
        ingestjob._stop.clear()

    full = "\n".join(logs.text)
    assert "сбой цикла" in full and "Traceback" in full
    assert "s3cr3t" not in full
