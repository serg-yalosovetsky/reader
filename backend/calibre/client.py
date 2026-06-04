"""Интеграция с Calibre на том же хосте (VPS: /root/calibre_lib).

- Добавление книг: `calibredb add --with-library <lib>` (CLI, subprocess).
- Чтение библиотеки: напрямую из metadata.db (SQLite) — легко и без запуска
  процесса Calibre; файлы книг лежат в <lib>/<Author>/<Title (id)>/<name>.<ext>.

Если Calibre не сконфигурирован (локальная разработка), функции деградируют:
add_book -> None, list_books -> [].
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

from ..app.config import CALIBRE_LIBRARY, CALIBREDB_BIN

_ADDED_RE = re.compile(r"Added book ids:\s*([\d,\s]+)")


def _library() -> Path | None:
    return Path(CALIBRE_LIBRARY) if CALIBRE_LIBRARY else None


def is_configured() -> bool:
    lib = _library()
    return bool(lib and (lib / "metadata.db").exists())


def add_book(file_path: str | Path) -> int | None:
    """Добавить файл в библиотеку Calibre. Возвращает calibre book id или None."""
    lib = _library()
    if not lib:
        return None
    try:
        proc = subprocess.run(
            [CALIBREDB_BIN, "add", "--with-library", str(lib), str(file_path)],
            capture_output=True, text=True, timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = _ADDED_RE.search(proc.stdout or "")
    if not m:
        return None
    ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
    return ids[0] if ids else None


def list_books() -> list[dict]:
    """Список книг из metadata.db: id, title, authors, путь, форматы."""
    lib = _library()
    if not lib or not (lib / "metadata.db").exists():
        return []
    db = lib / "metadata.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT b.id, b.title, b.path, b.has_cover,
                   IFNULL(GROUP_CONCAT(a.name, ' & '), '') AS authors
            FROM books b
            LEFT JOIN books_authors_link bal ON bal.book = b.id
            LEFT JOIN authors a ON a.id = bal.author
            GROUP BY b.id
            ORDER BY b.timestamp DESC
            """
        ).fetchall()
        fmt_rows = con.execute("SELECT book, format, name FROM data").fetchall()
    finally:
        con.close()

    formats: dict[int, list[dict]] = {}
    for fr in fmt_rows:
        formats.setdefault(fr["book"], []).append(
            {"format": fr["format"].lower(), "name": fr["name"]}
        )

    out = []
    for r in rows:
        out.append({
            "calibre_id": r["id"],
            "title": r["title"],
            "authors": r["authors"],
            "path": r["path"],
            "has_cover": bool(r["has_cover"]),
            "formats": formats.get(r["id"], []),
        })
    return out


def book_file_path(calibre_id: int, prefer=("epub", "fb2")) -> Path | None:
    """Путь к файлу книги нужного формата на диске."""
    lib = _library()
    if not lib:
        return None
    for book in list_books():
        if book["calibre_id"] != calibre_id:
            continue
        fmts = {f["format"]: f["name"] for f in book["formats"]}
        for ext in prefer:
            if ext in fmts:
                return lib / book["path"] / f"{fmts[ext]}.{ext}"
    return None
