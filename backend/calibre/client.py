"""Интеграция с Calibre — два режима.

HTTP/OPDS (боевой): задан CALIBRE_SERVER_URL → каталог и файлы книг берём через
OPDS calibre-web (напрямую по Tailscale, мимо nginx/SSO). На этапе каталога книга
НЕ копируется в ридер — хранится только ссылка (calibre_id + формат); сам файл
тянется по требованию при открытии (fetch-on-open) в evictable-кэш. Так нет
дублирования: книга физически живёт в Calibre, ридер держит ссылку + свой прогресс.

Локальный (dev/legacy): CALIBRE_LIBRARY указывает на каталог с metadata.db —
читаем SQLite напрямую и берём файлы с диска. Оставлен для локальной разработки.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

import httpx

# defusedxml защищает от XXE/billion-laughs. Источник OPDS — свой calibre-web по
# Tailscale (доверенный), но парсим безопасно на случай MITM/подмены.
from defusedxml import ElementTree as ET

if TYPE_CHECKING:
    # Только для аннотации типа: сам разбор идёт через defusedxml выше, а с
    # `from __future__ import annotations` аннотация в рантайме не вычисляется,
    # поэтому небезопасный парсер сюда не подтягивается. Раньше имени не было
    # вовсе — F821, годная приманка для правки «добавлю обычный импорт».
    from xml.etree.ElementTree import Element

from ..app.config import (
    CALIBRE_LIBRARY,
    CALIBRE_PASSWORD,
    CALIBRE_SERVER_URL,
    CALIBRE_USERNAME,
    CALIBREDB_BIN,
)

# --- OPDS namespaces / rel ---
_ATOM = "{http://www.w3.org/2005/Atom}"
_DC = "{http://purl.org/dc/terms/}"
_REL_ACQ = "http://opds-spec.org/acquisition"
_REL_IMG = "http://opds-spec.org/image"
_DL_RE = re.compile(r"/opds/download/(\d+)/([^/]+)/?")
_COVER_RE = re.compile(r"/opds/cover/(\d+)")
_PAGE = 60  # calibre-web config_books_per_page

# Предпочтение форматов: epub (нативно для foliate), затем fb2.
PREFER = ("epub", "fb2")

_ADDED_RE = re.compile(r"Added book ids:\s*([\d,\s]+)")


# ======================= режим =======================


def http_mode() -> bool:
    return bool(CALIBRE_SERVER_URL)


def is_configured() -> bool:
    if http_mode():
        return True
    lib = _library()
    return bool(lib and (lib / "metadata.db").exists())


# ======================= HTTP/OPDS =======================


def _base() -> str:
    return CALIBRE_SERVER_URL.rstrip("/")


def _client(timeout: float = 30.0) -> httpx.Client:
    auth = (CALIBRE_USERNAME, CALIBRE_PASSWORD) if CALIBRE_USERNAME else None
    return httpx.Client(
        base_url=_base(), auth=auth, timeout=timeout, follow_redirects=True
    )


def best_format(formats, prefer=PREFER) -> Optional[str]:
    """Выбрать лучший из доступных форматов книги. formats: dict|iterable форматов."""
    keys = formats.keys() if isinstance(formats, dict) else list(formats)
    for f in prefer:
        if f in keys:
            return f
    return next(iter(keys), None)


def _parse_dt(raw: str | None) -> Optional[datetime]:
    """ISO-8601 из OPDS (`2026-07-10T08:21:48+00:00`) -> naive UTC datetime."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_entry(e: Element) -> Optional[dict]:
    """Разобрать <entry> OPDS в словарь книги. None — если нечего скачивать."""
    title = (e.findtext(f"{_ATOM}title") or "").strip()
    author = ""
    ae = e.find(f"{_ATOM}author/{_ATOM}name")
    if ae is not None and ae.text:
        author = ae.text.strip()
    lang = (e.findtext(f"{_DC}language") or "").strip()
    desc = ""
    ce = e.find(f"{_ATOM}content")
    if ce is not None:
        desc = " ".join("".join(ce.itertext()).split()).strip()
    tags = []
    for cat in e.findall(f"{_ATOM}category"):
        lbl = cat.get("label") or cat.get("term")
        if lbl and lbl not in tags:
            tags.append(lbl)

    formats: dict[str, dict] = {}
    calibre_id: Optional[int] = None
    cover_id: Optional[int] = None
    for link in e.findall(f"{_ATOM}link"):
        rel = link.get("rel") or ""
        href = link.get("href") or ""
        if rel == _REL_ACQ:
            m = _DL_RE.search(href)
            if m:
                calibre_id = int(m.group(1))
                fmt = m.group(2).lower()
                formats[fmt] = {
                    "href": href,
                    "type": link.get("type") or "",
                    "length": int(link.get("length") or 0),
                }
        elif rel.startswith(_REL_IMG):
            m = _COVER_RE.search(href)
            if m:
                cover_id = int(m.group(1))

    if calibre_id is None:
        calibre_id = cover_id
    if calibre_id is None:
        return None
    # Дата добавления/правки книги в Calibre. Нужна, чтобы плановый синк не
    # выдавал давно лежащую в каталоге книгу за «только что появившуюся».
    added = _parse_dt(e.findtext(f"{_ATOM}updated"))
    return {
        "calibre_id": calibre_id,
        "updated": added,
        "title": title,
        "authors": author,
        "language": lang,
        "description": desc,
        "tags": tags,
        "formats": formats,  # {fmt: {href, type, length}}
        "has_cover": cover_id is not None,
        "cover_href": f"/opds/cover/{calibre_id}" if cover_id is not None else "",
    }


def iter_opds_books(page_limit: int = 2000) -> Iterator[dict]:
    """Пройти весь каталог calibre-web через /opds/new (offset-пагинация)."""
    with _client(timeout=60.0) as cli:
        offset = 0
        for _ in range(page_limit):
            params = {"offset": offset} if offset else None
            r = cli.get("/opds/new", params=params)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            entries = root.findall(f"{_ATOM}entry")
            if not entries:
                break
            for e in entries:
                d = _parse_entry(e)
                if d:
                    yield d
            if len(entries) < _PAGE:
                break
            offset += _PAGE


def download_book(calibre_id: int, fmt: str, dest: str | Path) -> tuple[Path, str]:
    """Скачать книгу формата fmt из Calibre в dest. Возвращает (path, sha1).

    Пишем во временный .part и атомарно переименовываем — недокачанный файл не
    попадёт в кэш. HTTP-режим.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha1()
    with _client(timeout=180.0) as cli:
        with cli.stream("GET", f"/opds/download/{calibre_id}/{fmt}/") as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    h.update(chunk)
                    f.write(chunk)
    tmp.replace(dest)
    return dest, h.hexdigest()


def cover_bytes(calibre_id: int) -> Optional[bytes]:
    """Байты обложки книги из Calibre (/opds/cover/{id}) или None."""
    try:
        with _client(timeout=30.0) as cli:
            r = cli.get(f"/opds/cover/{calibre_id}")
            if r.status_code == 200 and r.content:
                return r.content
    except httpx.HTTPError:
        pass
    return None


# ======================= локальный (legacy) =======================


def _library() -> Path | None:
    return Path(CALIBRE_LIBRARY) if CALIBRE_LIBRARY else None


def add_book(file_path: str | Path) -> int | None:
    """Добавить файл в локальную библиотеку Calibre (calibredb). Только dev-режим."""
    lib = _library()
    if not lib:
        return None
    try:
        proc = subprocess.run(
            [CALIBREDB_BIN, "add", "--with-library", str(lib), str(file_path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = _ADDED_RE.search(proc.stdout or "")
    if not m:
        return None
    ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
    return ids[0] if ids else None


def _list_books_local() -> list[dict]:
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
        out.append(
            {
                "calibre_id": r["id"],
                "updated": None,
                "title": r["title"],
                "authors": r["authors"],
                "path": r["path"],
                "has_cover": bool(r["has_cover"]),
                "formats": formats.get(r["id"], []),
            }
        )
    return out


# Обход каталога — это ~1400 книг двадцатью с лишним последовательными
# OPDS-запросами (60 записей на страницу), около 9 секунд. Фронт просит
# каталог при каждой загрузке библиотеки, поэтому держим результат в памяти:
# книги в Calibre появляются извне и редко, а промах по конкретному id
# обновляет кэш принудительно (см. force).
CATALOG_TTL = 300.0  # секунд
_catalog: list[dict] | None = None
_catalog_at = 0.0
# Лок, а не просто проверка времени: синхронные ручки FastAPI выполняются в
# threadpool, и без него пять одновременных запросов дали бы пять полных
# обходов каталога вместо одного.
_catalog_lock = threading.Lock()


def invalidate_catalog() -> None:
    """Забыть закэшированный каталог (следующий вызов сходит в Calibre)."""
    global _catalog, _catalog_at
    with _catalog_lock:
        _catalog, _catalog_at = None, 0.0


def list_books(force: bool = False) -> list[dict]:
    """Список книг: HTTP-режим — через OPDS; иначе — локальный metadata.db.

    force=True обходит кэш — нужно там, где свежесть важнее скорости
    (книгу только что добавили в Calibre, а мы её не видим).
    """
    if not http_mode():
        return _list_books_local()
    global _catalog, _catalog_at
    with _catalog_lock:
        fresh = _catalog is not None and (time.monotonic() - _catalog_at) < CATALOG_TTL
        if fresh and not force:
            return _catalog
        try:
            _catalog = list(iter_opds_books())
            _catalog_at = time.monotonic()
        except Exception:  # noqa: BLE001
            # Каталог недоступен — отдаём прошлый, если он есть: показать
            # устаревший список полезнее, чем пустой.
            return _catalog or []
        return _catalog


def book_file_path(calibre_id: int, prefer=PREFER) -> Path | None:
    """Путь к файлу книги на диске (только локальный режим)."""
    lib = _library()
    if not lib:
        return None
    for book in _list_books_local():
        if book["calibre_id"] != calibre_id:
            continue
        fmts = {f["format"]: f["name"] for f in book["formats"]}
        for ext in prefer:
            if ext in fmts:
                return lib / book["path"] / f"{fmts[ext]}.{ext}"
    return None
