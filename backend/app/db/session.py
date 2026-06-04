"""Инициализация БД и выдача сессий."""
from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from ..config import DB_URL, ensure_dirs

# check_same_thread=False — FastAPI/uvicorn могут дёргать из разных потоков;
# для SQLite это безопасно при коротких сессиях.
engine = create_engine(DB_URL, echo=False, connect_args={"check_same_thread": False})


def init_db() -> None:
    """Создать каталоги и таблицы. Импорт моделей обязателен до create_all."""
    ensure_dirs()
    from . import models  # noqa: F401  (регистрирует таблицы в метаданных)

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI: сессия на запрос."""
    with Session(engine) as session:
        yield session
