"""Test setup for the reader. Point every runtime path at a throwaway temp dir BEFORE
any backend module is imported, so tests never touch the live service's data / DB /
encryption key. Also provides an in-memory SQLite `session` fixture."""

from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="reader-tests-")
os.environ["READER_DATA_DIR"] = _tmp
os.environ["READER_BOOKS_DIR"] = os.path.join(_tmp, "books")
os.environ["READER_COVERS_DIR"] = os.path.join(_tmp, "covers")
os.environ["READER_TMP_DIR"] = os.path.join(_tmp, "tmp")
os.environ["READER_TTS_DIR"] = os.path.join(_tmp, "tts")
os.environ["READER_SECRET_KEY_PATH"] = os.path.join(_tmp, "secret.key")
os.environ["READER_DB_PATH"] = os.path.join(_tmp, "reader.db")
# Тесты — только на временном SQLite: снимаем боевой READER_DB_URL (Postgres),
# иначе тесты писали бы в прод-БД mesh-postgres.
os.environ.pop("READER_DB_URL", None)

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from backend.app.db import models  # noqa: E402,F401  (registers tables on metadata)


@pytest.fixture
def session():
    """Fresh in-memory SQLite DB with all reader tables created."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient. The scheduler is stubbed so lifespan starts no background
    jobs; lifespan's init_db() creates all tables in the throwaway tmp DB (READER_DB_PATH
    from the env set at the top of this file), so routers hit a real-but-empty database."""
    from fastapi.testclient import TestClient

    from backend.app import scheduler

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)

    from backend.app.main import app

    with TestClient(app) as c:
        yield c
