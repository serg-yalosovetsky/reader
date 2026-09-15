"""Причина отказа FanFicFare обязана доходить до сообщения об ошибке.

Живой случай 2026-09-15 (serg/tasks#888): в venv ридера стоит
`zzz_sentry_bootstrap.pth`, и он печатает «[sentry_bootstrap] Sentry initialized…»
в stderr КАЖДОГО python-процесса — включая подпроцесс FanFicFare. Сообщение об
ошибке строилось как `stderr or stdout`, поэтому настоящая причина
(«Story does not exist» в stdout) терялась: и в логе, и в интерфейсе висела
строка про Sentry.
"""

from __future__ import annotations

import pytest

from backend.downloaders import fanficfare_engine as fff
from backend.downloaders.base import DownloaderError

URL = "https://ficbook.net/readfic/018d35bc-6588-787a-a2d6-aefc1af2a348"
NOISE = "[sentry_bootstrap] Sentry initialized (env=production, max 5/3600s per error)"


def _fake_run(stdout: str, stderr: str):
    def run(*a, **k):
        return 0, stdout, stderr

    return run


def test_reason_from_stdout_is_not_masked_by_bootstrap_noise(monkeypatch):
    monkeypatch.setattr(
        fff, "_run_fff",
        _fake_run(f"Story does not exist: ({URL})\n", NOISE + "\n"),
    )
    with pytest.raises(DownloaderError) as ei:
        fff.download(URL)
    msg = str(ei.value)
    assert "Story does not exist" in msg
    assert "sentry_bootstrap" not in msg


def test_real_stderr_reason_is_kept(monkeypatch):
    monkeypatch.setattr(
        fff, "_run_fff",
        _fake_run("", NOISE + "\nHTTPErrorFFF: 403 Client Error: Forbidden\n"),
    )
    with pytest.raises(DownloaderError) as ei:
        fff.download(URL)
    msg = str(ei.value)
    assert "403" in msg
    assert "sentry_bootstrap" not in msg


def test_only_noise_still_says_something(monkeypatch):
    monkeypatch.setattr(fff, "_run_fff", _fake_run("", NOISE + "\n"))
    with pytest.raises(DownloaderError) as ei:
        fff.download(URL)
    msg = str(ei.value)
    assert "sentry_bootstrap" not in msg
    assert "EPUB не создан" in msg


def test_unknown_site_detection_survives_noise(monkeypatch):
    from backend.downloaders.base import UnsupportedURL

    monkeypatch.setattr(
        fff, "_run_fff",
        _fake_run("", NOISE + "\nFailed to find adapter for URL\n"),
    )
    with pytest.raises(UnsupportedURL):
        fff.download("https://example.org/story/1")
