"""Скан папки книг в Google Drive (gdrive:ReadEra/Books) и импорт новых книг.

Папка огромная (тысячи файлов), и сама читалка туда же заливает свои книги
(_push_readera). Поэтому: берём только недавно изменённые (AlicePhone кладёт
файл -> свежий modtime), дедупим по sha1 (свои заливки уже в библиотеке),
чёрный список, лимит и dry-run по умолчанию.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from . import blacklist, covers
from .config import RCLONE_BIN, READERA_BOOKS_REMOTE
from .db.models import Work, utcnow
from .storage import detect_format, import_file, sha1_of_file

_EXT = (".epub", ".fb2", ".fb2.zip", ".zip")


def _recent(days: int) -> list[tuple[str, datetime, int]]:
    if not READERA_BOOKS_REMOTE:
        return []
    p = subprocess.run([RCLONE_BIN, "lsjson", READERA_BOOKS_REMOTE, "-R", "--files-only"],
                       capture_output=True, text=True, timeout=180)
    try:
        items = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return []
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for it in items:
        name = str(it.get("Name", "")).lower()
        if not name.endswith(_EXT):
            continue
        try:
            dt = datetime.fromisoformat(str(it.get("ModTime", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cut:
            out.append((it.get("Path", it.get("Name", "")), dt, it.get("Size", 0)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _extract_inner(p: Path) -> Path:
    """Если .zip/.fb2.zip — достать первый .fb2/.epub наружу."""
    if p.suffix.lower() != ".zip":
        return p
    try:
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".fb2", ".epub"))]
            if not names:
                return p
            dest = p.parent / Path(names[0]).name
            with z.open(names[0]) as src, open(dest, "wb") as out:
                out.write(src.read())
            return dest
    except zipfile.BadZipFile:
        return p


def _meta(path: Path, fmt: str) -> tuple[str, str]:
    title = author = ""
    try:
        if fmt == "epub":
            with zipfile.ZipFile(path) as z:
                opf = [n for n in z.namelist() if n.lower().endswith(".opf")]
                if opf:
                    x = z.read(opf[0]).decode("utf-8", "ignore")
                    mt = re.search(r"<dc:title[^>]*>([^<]+)<", x)
                    ma = re.search(r"<dc:creator[^>]*>([^<]+)<", x)
                    title = mt.group(1).strip() if mt else ""
                    author = ma.group(1).strip() if ma else ""
        elif fmt == "fb2":
            with open(path, encoding="utf-8", errors="ignore") as fh:
                x = fh.read(20000)
            mt = re.search(r"<book-title>([^<]+)</book-title>", x)
            fn = re.search(r"<first-name>([^<]*)</first-name>", x)
            ln = re.search(r"<last-name>([^<]*)</last-name>", x)
            title = mt.group(1).strip() if mt else ""
            author = " ".join(p for p in [
                fn.group(1).strip() if fn else "", ln.group(1).strip() if ln else ""] if p).strip()
    except Exception:  # noqa: BLE001
        pass
    return title, author


def scan(session: Session, days: int = 7, limit: int = 30, commit: bool = False) -> dict:
    recent = _recent(days)[:limit]
    res: dict = {"recent_count": len(recent), "imported": [], "skipped_existing": 0,
                 "skipped_blacklist": [], "errors": []}
    if not commit:
        res["candidates"] = [{"path": p, "modtime": dt.isoformat(), "size": sz}
                             for p, dt, sz in recent]
        return res
    tmp = Path(tempfile.mkdtemp(prefix="drvbooks_"))
    for path, _dt, _sz in recent:
        try:
            local = tmp / Path(path).name
            subprocess.run([RCLONE_BIN, "copyto", f"{READERA_BOOKS_REMOTE}/{path}", str(local)],
                           timeout=300, check=True)
            book = _extract_inner(local)
            fmt = detect_format(book.name) or ""
            if not fmt:
                res["errors"].append({"path": path, "err": "unknown format"})
                continue
            sha1 = sha1_of_file(book)
            if session.exec(select(Work).where(Work.sha1 == sha1)).first():
                res["skipped_existing"] += 1
                continue
            title, author = _meta(book, fmt)
            title = title or Path(path).stem
            if blacklist.is_blacklisted(session, title=title, author=author):
                res["skipped_blacklist"].append(title)
                continue
            dest, _ = import_file(book, sha1)
            cov = None
            try:
                cov = covers.extract_cover(dest, fmt, sha1)
            except Exception:  # noqa: BLE001
                cov = None
            w = Work(title=title, author=author, site="drive-books", source_url="",
                     sha1=sha1, file_path=str(dest), file_format=fmt,
                     cover_path=str(cov) if cov else None,
                     created_at=utcnow(), updated_at=utcnow())
            session.add(w)
            session.commit()
            session.refresh(w)
            res["imported"].append({"id": w.id, "title": title})
        except Exception as e:  # noqa: BLE001
            res["errors"].append({"path": path, "err": str(e)[:200]})
    return res
