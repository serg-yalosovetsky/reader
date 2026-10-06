"""Дочерний FanFicFare запускается БЕЗ инструментовки сервера (serg/tasks#1525).

Сервис стартует под `opentelemetry-instrument`, и через PYTHONPATH
(`.../auto_instrumentation`) плюс OTEL_* / SENTRY_DSN каждый дочерний python
поднимал OTel-дистро и Sentry: одна ficbook-ссылка get_meta — 3.94 с CPU против
1.39 с в чистом окружении. Чистится только инструментовка; прокси, READER_* и
прочее окружение остаются (запрещающий список, а не разрешающий).
"""

from __future__ import annotations

import os
import subprocess

from backend.downloaders import fanficfare_engine as fff

AUTO = "/opt/reader/.venv/lib/python3.12/site-packages/opentelemetry/instrumentation/auto_instrumentation"


def _server_env(monkeypatch, pythonpath: str | None) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "reader")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "otlp")
    monkeypatch.setenv("SENTRY_DSN", "https://key@glitchtip.example/2")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "production")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("HTTP_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("READER_AT_PROXY", "socks5://127.0.0.1:1081")
    if pythonpath is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", pythonpath)


def test_child_env_drops_instrumentation(monkeypatch):
    _server_env(monkeypatch, os.pathsep.join([AUTO, "/opt/reader"]))
    env = fff._child_env()
    assert "SENTRY_DSN" not in env
    assert not [k for k in env if k.startswith("OTEL_")]
    assert "auto_instrumentation" not in env.get("PYTHONPATH", "")


def test_child_env_keeps_everything_else(monkeypatch):
    _server_env(monkeypatch, os.pathsep.join([AUTO, "/opt/reader"]))
    env = fff._child_env()
    assert env["PYTHONPATH"] == "/opt/reader"
    assert env["HTTPS_PROXY"] == "socks5://127.0.0.1:1080"
    assert env["HTTP_PROXY"] == "socks5://127.0.0.1:1080"
    assert env["READER_AT_PROXY"] == "socks5://127.0.0.1:1081"
    # Denylist: всё, что не инструментовка, переезжает как есть.
    for key, value in os.environ.items():
        if key in ("SENTRY_DSN", "PYTHONPATH") or key.startswith("OTEL_"):
            continue
        assert env[key] == value, key


def test_child_env_removes_pythonpath_with_only_instrumentation(monkeypatch):
    _server_env(monkeypatch, AUTO)
    assert "PYTHONPATH" not in fff._child_env()


def test_child_env_without_pythonpath(monkeypatch):
    _server_env(monkeypatch, None)
    assert "PYTHONPATH" not in fff._child_env()


def test_child_env_does_not_mutate_server_env(monkeypatch):
    _server_env(monkeypatch, os.pathsep.join([AUTO, "/opt/reader"]))
    fff._child_env()
    assert os.environ["SENTRY_DSN"]
    assert AUTO in os.environ["PYTHONPATH"]


def test_get_meta_and_download_pass_child_env(monkeypatch, tmp_path):
    """Оба места запуска FanFicFare получают очищенное окружение."""
    _server_env(monkeypatch, os.pathsep.join([AUTO, "/opt/reader"]))
    seen: list[dict] = []

    def fake_run(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"numChapters": 3}', stderr="")

    monkeypatch.setattr(fff.subprocess, "run", fake_run)
    assert fff.get_meta("https://ficbook.net/readfic/1")["numChapters"] == 3

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen.append(kw.get("env"))
            import io
            self.stdout = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(fff.subprocess, "Popen", FakePopen)
    fff._run_fff(["x"], tmp_path, 10, total=None)

    assert len(seen) == 2
    for env in seen:
        assert env is not None, "дочерний процесс унаследовал окружение сервера"
        assert "SENTRY_DSN" not in env
        assert not [k for k in env if k.startswith("OTEL_")]
        assert "auto_instrumentation" not in env.get("PYTHONPATH", "")
        assert env["HTTPS_PROXY"] == "socks5://127.0.0.1:1080"


def test_get_meta_runs_in_a_throwaway_cwd(monkeypatch):
    """`-m` пишет пустой EPUB в cwd: он не должен копиться в каталоге сервиса."""
    cwds: list[str] = []

    def fake_run(cmd, **kw):
        cwd = kw.get("cwd")
        cwds.append(cwd)
        assert cwd and os.path.isdir(cwd)
        open(os.path.join(cwd, "Book-fbn_1.epub"), "wb").close()
        return subprocess.CompletedProcess(cmd, 0, stdout='{"numChapters": 1}', stderr="")

    monkeypatch.setattr(fff.subprocess, "run", fake_run)
    fff.get_meta("https://ficbook.net/readfic/1")
    assert cwds and os.path.abspath(cwds[0]) != os.getcwd()
    assert not os.path.exists(cwds[0]), "временный каталог get_meta не удалён"
