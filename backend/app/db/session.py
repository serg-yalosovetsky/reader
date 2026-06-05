"""Инициализация БД и выдача сессий."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from ..config import DB_URL, ensure_dirs

# check_same_thread=False — FastAPI/uvicorn могут дёргать из разных потоков;
# для SQLite это безопасно при коротких сессиях.
engine = create_engine(DB_URL, echo=False, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
    """WAL + busy_timeout: параллельные чтения/записи не дают 'database is locked'
    (мониторинг с докачкой работает долго, а фронт параллельно опрашивает API)."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db() -> None:
    """Создать каталоги и таблицы. Импорт моделей обязателен до create_all."""
    ensure_dirs()
    from . import models  # noqa: F401  (регистрирует таблицы в метаданных)

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI: сессия на запрос."""
    with Session(engine) as session:
        yield session
