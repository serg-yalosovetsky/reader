"""Один процесс FanFicFare на фазу счёта глав (serg/tasks#1541, шаг B #1525).

Без сети: вместо FanFicFare драйвер (настоящий `serve()` из fff_meta_driver.py,
с настоящим разведением протокольных дескрипторов) зовёт поддельный `main`,
поведение которого задаёт URL: ok / hang / die / exit2 / boom / noise / argv.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.accounts import monitor
from backend.downloaders import fanficfare_engine as fff

DRIVER_DIR = Path(fff.__file__).parent

FAKE = textwrap.dedent(
    """
    import json, os, resource, sys, time
    sys.path.insert(0, {driver_dir!r})
    import fff_meta_driver as d

    def main(argv):
        url = argv[-1]
        if "hang" in url:
            time.sleep(3600)
        if "die" in url:
            os._exit(9)
        if "exit2" in url:
            sys.exit(2)
        if "boom" in url:
            raise ValueError("boom")
        if "noise" in url:
            os.write(1, b"garbage straight to fd 1\\n")
            sys.stdin.read()  # интерактивный вопрос FanFicFare не должен съесть задание
        if "argv" in url:
            print(json.dumps({{
                "argv": argv, "pid": os.getpid(), "cwd": os.getcwd(),
                "sentry": "SENTRY_DSN" in os.environ,
                "as": resource.getrlimit(resource.RLIMIT_AS)[0],
            }}))
            return
        if "junk" in url:
            print("junk before " + json.dumps({{"numChapters": 9}}) + " junk after")
            return
        print(json.dumps({{"numChapters": 5, "pid": os.getpid()}}))

    d._limit_memory()
    pin, pout = d._detach_std_streams()
    d._reply(pout, {{"ready": True, "pid": os.getpid(), "rss_kb": d._rss_kb()}})
    sys.exit(d.serve(main, pin, pout))
    """
)


@pytest.fixture
def fake_cmd(tmp_path):
    script = tmp_path / "fake_driver.py"
    script.write_text(FAKE.format(driver_dir=str(DRIVER_DIR)), encoding="utf-8")
    return [sys.executable, str(script)]


def _drv(fake_cmd, **kw):
    kw.setdefault("pause", {})
    kw.setdefault("as_mb", 0)
    return fff.MetaDriver(cmd=fake_cmd, **kw)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Зомби ещё «жив» для kill(0); wait() в _stop его пожинает, так что здесь его нет.
    return True


def test_many_urls_one_process(fake_cmd):
    with _drv(fake_cmd, max_jobs=10) as d:
        pids = {d.get_meta(f"https://ficbook.net/readfic/ok{i}")["pid"] for i in range(4)}
        assert d.stats["starts"] == 1 and d.stats["jobs"] == 4
    assert len(pids) == 1


def test_result_same_as_get_meta(fake_cmd, monkeypatch):
    """Ответ драйвера разбирается ровно как ответ отдельного процесса get_meta."""
    import subprocess

    stdout = "junk before " + json.dumps({"numChapters": 9}) + " junk after"
    monkeypatch.setattr(
        fff.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""),
    )
    with _drv(fake_cmd) as d:
        assert d.get_meta("https://ficbook.net/readfic/junk") == fff.get_meta(
            "https://ficbook.net/readfic/junk") == {"numChapters": 9}


def test_argv_env_cwd_rlimit(fake_cmd):
    """Аргументы те же, что у get_meta; окружение — _child_env; cwd временный; RLIMIT_AS стоит."""
    os.environ["SENTRY_DSN"] = "https://k@glitchtip.example/1"
    try:
        with _drv(fake_cmd, as_mb=300) as d:
            url = "https://ficbook.net/readfic/argv"
            got = d.get_meta(url, creds=("u", "p"), extra={"k": "v"})
            cwd = got["cwd"]
    finally:
        del os.environ["SENTRY_DSN"]
    argv = got["argv"]
    assert argv[: len(fff._meta_args(url, {"k": "v"}))] == fff._meta_args(url, {"k": "v"})
    assert argv[-1] == url and argv[-3] == "-c"
    assert "p" not in argv, "пароль попал в argv"
    assert got["sentry"] is False
    assert got["as"] == 300 * 1024 * 1024
    assert os.path.abspath(cwd) != os.getcwd()
    assert not os.path.exists(cwd), "временный cwd драйвера не удалён"


def test_restart_every_n_jobs(fake_cmd, caplog):
    caplog.set_level(logging.INFO, logger="reader.download")
    with _drv(fake_cmd, max_jobs=2) as d:
        pids = [d.get_meta(f"https://ficbook.net/readfic/ok{i}")["pid"] for i in range(5)]
        assert d.stats["starts"] == 3
    assert pids[0] == pids[1] != pids[2] == pids[3] != pids[4]
    assert not _alive(pids[0]) and not _alive(pids[2])
    assert sum("плановый перезапуск" in r.message for r in caplog.records) == 2


def test_hang_kills_driver_and_next_url_works(fake_cmd, caplog):
    caplog.set_level(logging.INFO, logger="reader.download")
    with _drv(fake_cmd, timeout=1) as d:
        first = d.get_meta("https://ficbook.net/readfic/ok0")["pid"]
        t0 = time.monotonic()
        assert d.get_meta("https://ficbook.net/readfic/hang1") == {}
        assert time.monotonic() - t0 < 10
        assert not _alive(first), "зависший драйвер не убит"
        second = d.get_meta("https://ficbook.net/readfic/ok2")["pid"]
        assert second != first
        assert d.stats["failures"] == 1
    warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("hang1" in m and "завис" in m for m in warns), warns


def test_crash_is_reported_and_driver_restarts(fake_cmd, caplog):
    with _drv(fake_cmd) as d:
        assert d.get_meta("https://ficbook.net/readfic/die1") == {}
        assert d.get_meta("https://ficbook.net/readfic/ok2")["numChapters"] == 5
        assert d.stats["starts"] == 2
    warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("die1" in m and "умер" in m for m in warns), warns


def test_failed_url_is_empty_and_logged_driver_survives(fake_cmd, caplog):
    """Отказ ссылки (SystemExit без вывода, исключение) — {} и warning, как у get_meta;
    драйвер при этом живёт дальше."""
    with _drv(fake_cmd) as d:
        pid = d.get_meta("https://ficbook.net/readfic/ok0")["pid"]
        assert d.get_meta("https://ficbook.net/readfic/exit2") == {}
        assert d.get_meta("https://ficbook.net/readfic/boom") == {}
        assert d.get_meta("https://ficbook.net/readfic/ok3")["pid"] == pid
    warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("exit2" in m and "код возврата 2" in m for m in warns), warns
    assert any("boom" in m and "ValueError" in m for m in warns), warns


def test_stray_fd_writes_and_stdin_reads_do_not_break_protocol(fake_cmd):
    with _drv(fake_cmd) as d:
        pid = d.get_meta("https://ficbook.net/readfic/ok0")["pid"]
        assert d.get_meta("https://ficbook.net/readfic/noise")["numChapters"] == 5
        assert d.get_meta("https://ficbook.net/readfic/ok2")["pid"] == pid
        assert d.stats["failures"] == 0


def test_driver_that_cannot_start_falls_back_to_process(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(fff, "get_meta", lambda url, **kw: calls.append(url) or {"numChapters": 7})
    broken = [sys.executable, "-c", "import sys; sys.exit(1)"]
    with fff.MetaDriver(cmd=broken, pause={}, as_mb=0, start_timeout=5) as d:
        got = [d.get_meta(f"https://ficbook.net/readfic/{i}") for i in range(5)]
        assert d.stats["fallback"] == 5 and d.stats["starts"] == 0
    assert got == [{"numChapters": 7}] * 5 and len(calls) == 5
    warns = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("не запустился" in m for m in warns) == 3, "после трёх отказов драйвер не выключен"
    assert any("выключен до конца серии" in m for m in warns)


def test_explicit_pause_between_ficbook_jobs(fake_cmd):
    with _drv(fake_cmd, pause={"ficbook.net": 0.6}) as d:
        d.get_meta("https://ficbook.net/readfic/ok0")
        t0 = time.monotonic()
        d.get_meta("https://sufficientvelocity.com/threads/ok1")
        other = time.monotonic() - t0
        d.get_meta("https://ficbook.net/readfic/ok2")
        ficbook = time.monotonic() - t0
    assert other < 0.5, "пауза ficbook задела другой сайт"
    assert ficbook >= 0.6, "между ficbook-заданиями нет паузы"


def test_chapter_count_uses_series_driver(monkeypatch):
    """Фаза счёта глав: FanFicFare-ссылки идут в драйвер серии, не в отдельный процесс."""
    def no_process(*a, **kw):
        raise AssertionError("отдельный процесс get_meta при наличии драйвера")

    monkeypatch.setattr(fff, "get_meta", no_process)

    class Drv:
        def __init__(self):
            self.urls = []

        def get_meta(self, url, *, creds=None):
            self.urls.append(url)
            return {"numChapters": "12"}

    d = Drv()
    assert monitor._chapter_count("https://ficbook.net/readfic/1", "ficbook.net", None, d) == 12
    assert d.urls == ["https://ficbook.net/readfic/1"]


def test_check_all_uses_one_driver_for_the_phase(session, monkeypatch):
    from backend.app.db.models import Monitored

    for i in range(3):
        session.add(Monitored(source_url=f"https://ficbook.net/readfic/{i}", last_seen_chapters=5))
    session.commit()
    monkeypatch.setattr(monitor.store, "creds_for_host", lambda s, h: None)
    monkeypatch.setattr(monitor, "_at_task", lambda t: None)

    made = []

    class Drv:
        def __init__(self):
            made.append(self)
            self.urls, self.closed = [], False
            self.stats = {"jobs": 0, "starts": 1, "failures": 0, "fallback": 0, "max_rss_kb": 0}

        def __enter__(self):
            return self

        def __exit__(self, *e):
            self.closed = True

        def get_meta(self, url, *, creds=None):
            assert not self.closed
            self.urls.append(url)
            return {"numChapters": 5}

    monkeypatch.setattr(monitor.fff, "MetaDriver", Drv)
    res = monitor.check_all(session, auto_download=False, pull_feeds=False)
    assert res["checked"] == 3
    assert len(made) == 1 and made[0].closed
    assert sorted(made[0].urls) == [f"https://ficbook.net/readfic/{i}" for i in range(3)]


def test_count_task_exception_is_logged(monkeypatch, caplog):
    """serg/tasks#1537: исключение счёта глав — None И строка в логе, не тишина."""
    def boom(*a, **kw):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(monitor, "_chapter_count", boom)
    task = {"url": "https://ficbook.net/readfic/x", "host": "ficbook.net", "creds": None}
    assert monitor._count_chapters_task(task) is None
    assert any("readfic/x" in r.message and "сломалось" in r.message for r in caplog.records)
