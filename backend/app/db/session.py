"""Инициализация БД и выдача сессий."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from ..config import DB_URL, ensure_dirs

_IS_SQLITE = DB_URL.startswith("sqlite")

if _IS_SQLITE:
    # check_same_thread=False — FastAPI/uvicorn могут дёргать из разных потоков;
    # для SQLite это безопасно при коротких сессиях.
    engine = create_engine(
        DB_URL, echo=False, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """WAL + busy_timeout: параллельные чтения/записи не дают 'database is
        locked' (мониторинг с докачкой работает долго, а фронт параллельно
        опрашивает API)."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
else:
    # Postgres (mesh-postgres): pool_pre_ping отбраковывает мёртвые соединения
    # (докачки держат сессию долго; сервер мог закрыть idle-коннект).
    engine = create_engine(DB_URL, echo=False, pool_pre_ping=True)


def init_db() -> None:
    """Создать каталоги и таблицы. Импорт моделей обязателен до create_all."""
    ensure_dirs()
    from . import models  # noqa: F401  (регистрирует таблицы в метаданных)

    SQLModel.metadata.create_all(engine)
    _migrate_add_columns()


def _migrate_add_columns() -> None:
    """Лёгкая авто-миграция: create_all не добавляет колонки в уже существующие
    таблицы, поэтому для новых полей моделей досоздаём их через ALTER TABLE."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # только что создана create_all — колонки полные
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(engine.dialect)
                default = ""
                if (
                    col.default is not None
                    and getattr(col.default, "arg", None) is not None
                    and not callable(col.default.arg)
                ):
                    val = col.default.arg
                    default = (
                        f" DEFAULT {val!r}"
                        if isinstance(val, str)
                        else f" DEFAULT {val}"
                    )
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{default}'
                    )
                )


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI: сессия на запрос."""
    with Session(engine) as session:
        yield session
