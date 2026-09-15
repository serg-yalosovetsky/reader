"""Прогресс фоновых скачиваний для панели «Скачивания» (serg/tasks#893).

Панель показывает, что качается и насколько продвинулось. Источник — строка
задания в `ingest_job` (#892), поэтому прогресс переживает перезагрузку страницы и
рестарт сервиса. Пишут его адаптеры: readli — по страницам пагинации, author.today —
по главам, FanFicFare — по точкам флага -p (одна точка на сетевой запрос; число
глав — из предварительного прохода --meta-only). Запись в общий Postgres не
чаще раза в 2 с на задание, чтобы панель не стала источником нагрузки.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from backend.app import ingestjob, progress
from backend.app.db.models import IngestJob
from backend.app.db.session import engine, init_db
from backend.downloaders import authortoday, readli
from backend.downloaders import fanficfare_engine as fff


@pytest.fixture
def queue_db():
    init_db()
    ingestjob.stop_workers(mark=False)
    with Session(engine) as s:
        for row in s.exec(select(IngestJob)).all():
            s.delete(row)
        s.commit()
    yield


def _put(**kw) -> str:
    job = IngestJob(**kw)
    with Session(engine) as s:
        s.add(job)
        s.commit()
        return job.id


def _row(job_id: str) -> IngestJob:
    with Session(engine) as s:
        row = s.get(IngestJob, job_id)
        s.expunge(row)
        return row


# --- Репортёр ---


def test_report_outside_a_job_does_nothing():
    progress.report(1, 2, "pages", "скачивание")  # синхронный POST, докачка монитором


def test_report_writes_progress_of_running_job(queue_db):
    job_id = _put(query="q", status="running")
    with progress.activate(job_id):
        progress.report(3, 10, "pages", "скачивание", title="Великие Спящие")
    row = _row(job_id)
    assert (row.progress_done, row.progress_total, row.progress_unit) == (3, 10, "pages")
    assert row.progress_stage == "скачивание"
    assert row.title == "Великие Спящие"
    assert row.heartbeat_at is not None


def test_report_is_throttled_but_last_step_is_always_written(queue_db):
    job_id = _put(query="q", status="running")
    with progress.activate(job_id):
        progress.report(1, 10, "pages", "скачивание")
        progress.report(2, 10, "pages")  # сразу за первым — пропускается
        assert _row(job_id).progress_done == 1
        progress.report(10, 10, "pages")  # последний шаг — всегда
    assert _row(job_id).progress_done == 10


def test_report_does_not_touch_finished_job(queue_db):
    job_id = _put(query="q", status="done", progress_done=16, progress_total=16)
    with progress.activate(job_id):
        progress.report(1, 10, "pages", "скачивание")
    row = _row(job_id)
    assert (row.progress_done, row.progress_total) == (16, 16)


# --- FanFicFare: точки -p — прогресс, а не текст ---


def test_fff_dots_become_progress_and_do_not_leak_into_reason(queue_db, monkeypatch):
    monkeypatch.setattr(progress, "MIN_INTERVAL_S", 0.0)
    job_id = _put(query="q", status="running")
    script = (
        "import sys, time\n"
        "sys.stdout.write('.....'); sys.stdout.flush(); time.sleep(1.4)\n"
        "sys.stdout.write('..Story does not exist: (https://ficbook.net/readfic/1)\\n')\n"
    )
    with progress.activate(job_id):
        rc, out, err = fff._run_fff([sys.executable, "-c", script], Path("."), 30, total=None)
    assert rc == 0
    assert out.strip() == "Story does not exist: (https://ficbook.net/readfic/1)"
    row = _row(job_id)
    assert row.progress_unit == "requests"
    assert row.progress_done >= 5


def test_fff_dots_map_to_chapters_when_total_known(queue_db, monkeypatch):
    monkeypatch.setattr(progress, "MIN_INTERVAL_S", 0.0)
    job_id = _put(query="q", status="running")
    # 2 запроса на страницу истории/метаданные + 3 главы
    script = "import sys, time\nsys.stdout.write('.....'); sys.stdout.flush(); time.sleep(1.4)\n"
    with progress.activate(job_id):
        fff._run_fff([sys.executable, "-c", script], Path("."), 30, total=3)
    row = _row(job_id)
    assert (row.progress_done, row.progress_total, row.progress_unit) == (3, 3, "chapters")


def test_fff_timeout_kills_the_process():
    script = "import time\ntime.sleep(30)\n"
    with pytest.raises(subprocess.TimeoutExpired):
        fff._run_fff([sys.executable, "-c", script], Path("."), 1, total=None)


# --- Адаптеры отчитываются ---


def test_readli_reports_pages(monkeypatch):
    calls: list = []
    monkeypatch.setattr(readli.progress, "report", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(readli, "_get", lambda c, url, attempts=4: SimpleNamespace(status_code=200, text="<html></html>"))
    monkeypatch.setattr(readli, "_parse_head", lambda soup: ("Книга", 3))
    monkeypatch.setattr(readli, "_parse_author", lambda soup: "Автор")
    monkeypatch.setattr(readli, "_page_html", lambda soup: "<h3>Глава 1</h3><p>текст</p>")
    monkeypatch.setattr(readli, "build_epub", lambda *a, **k: Path("/tmp/readli.epub"))
    monkeypatch.setattr(readli.time, "sleep", lambda s: None)
    from backend.app import covers

    monkeypatch.setattr(covers, "fetch_cover_bytes", lambda url: None)
    readli.download("https://readli.net/chitat-online/?b=1")
    assert calls, "readli не сообщил прогресс"
    first_args, first_kw = calls[0]
    assert first_args[:3] == (1, 3, "pages") and first_kw.get("title") == "Книга"
    assert calls[-1][0][:3] == (3, 3, "pages")


def test_authortoday_reports_chapters(monkeypatch):
    calls: list = []
    monkeypatch.setattr(authortoday.progress, "report", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(authortoday.egress, "at_client", lambda **kw: contextlib.nullcontext(object()))
    monkeypatch.setattr(authortoday, "_get", lambda c, url, **kw: SimpleNamespace(status_code=200, text=""))
    monkeypatch.setattr(authortoday, "_parse_work_meta", lambda html: ("Книга", "Автор", "", {}))
    monkeypatch.setattr(authortoday, "_is_free", lambda html: True)
    monkeypatch.setattr(
        authortoday, "_parse_chapters",
        lambda html: [{"id": 1, "title": "Глава 1"}, {"id": 2, "title": "Глава 2"}],
    )
    monkeypatch.setattr(authortoday, "_fetch_chapter", lambda c, w, ch, u: "<p>текст</p>")
    monkeypatch.setattr(authortoday, "_build_epub", lambda *a, **k: Path("/tmp/at.epub"))
    monkeypatch.setattr(authortoday.time, "sleep", lambda s: None)
    from backend.app import covers

    monkeypatch.setattr(covers, "fetch_cover_bytes", lambda url: None)
    authortoday._download_once("https://author.today/work/1")
    assert calls[0][0][:3] == (0, 2, "chapters") and calls[0][1].get("title") == "Книга"
    assert calls[-1][0][:3] == (2, 2, "chapters")


# --- Список заданий для панели ---


def test_list_jobs_shows_active_first_then_recent(queue_db):
    t = ingestjob.now()
    a = _put(query="a", status="running", created_at=t - timedelta(minutes=5), started_at=t)
    b = _put(query="b", status="queued", created_at=t - timedelta(minutes=1))
    c = _put(query="c", status="done", created_at=t - timedelta(hours=1), finished_at=t - timedelta(minutes=30))
    _put(query="d", status="done", created_at=t - timedelta(days=3), finished_at=t - timedelta(days=3))
    data = ingestjob.list_jobs(limit=20)
    assert data["active"] == 2
    assert [j["job_id"] for j in data["jobs"]] == [a, b, c]
    assert data["jobs"][0]["progress"] == {"done": 0, "total": None, "unit": "", "stage": ""}


def test_api_jobs_list(client):
    r = client.get("/api/ingest/jobs?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"active", "jobs"}
