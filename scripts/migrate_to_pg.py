"""Миграция данных reader: SQLite -> Postgres (mesh-postgres).

Читаем через типизированные модели SQLModel (Boolean/DateTime конвертируются
корректно на обеих сторонах), пишем в Postgres, сбрасываем sequence PK.
Идемпотентно: TRUNCATE перед копированием. SRC — файл SQLite, DST — READER_DB_URL.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, insert, select, text
from sqlmodel import SQLModel

sys.path.insert(0, "/root/reader")
import backend.app.db.models  # noqa: F401  (регистрирует таблицы в metadata)

SRC_URL = os.getenv("SRC_DB_URL", "sqlite:////root/reader/data/reader.db")
DST_URL = os.environ["READER_DB_URL"]
assert DST_URL.startswith("postgresql"), f"DST не postgres: {DST_URL[:20]}"

src = create_engine(SRC_URL, connect_args={"check_same_thread": False})
dst = create_engine(DST_URL)

SQLModel.metadata.create_all(dst)
tables = SQLModel.metadata.sorted_tables

with src.connect() as s, dst.begin() as d:
    # 1) чистим приёмник (обратный порядок — уважаем FK)
    for t in reversed(tables):
        d.execute(text(f'TRUNCATE TABLE "{t.name}" RESTART IDENTITY CASCADE'))
    # 2) копируем строки
    report = {}
    for t in tables:
        rows = [dict(r._mapping) for r in s.execute(select(t))]
        if rows:
            d.execute(insert(t), rows)
        report[t.name] = len(rows)
    # 3) чиним sequence для integer-PK
    for t in tables:
        pk = list(t.primary_key.columns)
        if len(pk) != 1:
            continue
        col = pk[0]
        seq = d.execute(
            text("SELECT pg_get_serial_sequence(:tbl, :col)"),
            {"tbl": f"reader.{t.name}", "col": col.name},
        ).scalar()
        if seq:
            d.execute(
                text(
                    f'SELECT setval(:seq, (SELECT COALESCE(MAX("{col.name}"), 1) '
                    f'FROM "{t.name}"))'
                ),
                {"seq": seq},
            )

# 4) верификация: сверяем счётчики
print("=== migrated (src -> dst) ===")
ok = True
with src.connect() as s, dst.connect() as d:
    for t in tables:
        sc = s.execute(text(f'SELECT count(*) FROM "{t.name}"')).scalar()
        dc = d.execute(text(f'SELECT count(*) FROM "{t.name}"')).scalar()
        flag = "OK" if sc == dc else "!!! MISMATCH"
        if sc != dc:
            ok = False
        print(f"  {t.name:12} {sc:>5} -> {dc:>5}  {flag}")
print("RESULT:", "ALL MATCH" if ok else "MISMATCH — проверь")
sys.exit(0 if ok else 1)
