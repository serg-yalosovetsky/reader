"""Перепоиск настоящей обложки для КАЖДОЙ книги, у которой её ещё нет: ИИ-обложка
(generated/gen_failed), Calibre-плейсхолдер, пустая или дженерик-файл. Полная
цепочка: встроенная в файл → сайт-источник/зеркала (ficbook/author.today/
searchfloor) → первая страница PDF (pdftoppm). Настоящую → ставим, старую заменяем.

Книги с уже настоящей обложкой (embedded/source/pdf/description) пропускаются.

Запуск (с загруженным .env → Postgres!):
    set -a; . ./.env; set +a
    PYTHONPATH=/root/reader .venv/bin/python scripts/rerun_covers.py [work_id ...]
"""

from __future__ import annotations

import os
import sys

from sqlmodel import Session, select

from backend.app import covers
from backend.app.db.models import Work
from backend.app.db.session import engine

# Источники обложки, которые считаем «ненастоящими» — их вытесняем.
_PLACEHOLDER_SOURCES = ("generated", "gen_failed", "calibre", "")


def _needs_cover(w: Work) -> bool:
    if w.cover_source in _PLACEHOLDER_SOURCES:
        return True
    if not w.cover_path or not os.path.exists(w.cover_path):
        return True
    try:
        return covers.is_generic_cover(open(w.cover_path, "rb").read())
    except OSError:
        return True


def _real_cover(w: Work):
    sha = w.sha1 or (f"cal{w.calibre_id}" if w.calibre_id else f"w{w.id}")
    # 1) встроенная в файл книги (epub/fb2).
    if w.file_path and os.path.exists(w.file_path):
        c = covers.extract_cover(w.file_path, w.file_format, w.sha1)
        if c:
            return c, "embedded"
    # 2) сайт-источник + зеркала на других сайтах по title/author.
    if w.source_url or w.title:
        c = covers.fetch_source_cover(w.source_url or "", sha, w.title, w.author)
        if c:
            return c, "source"
    # 3) первая страница PDF (Calibre-книги без сохранённой обложки).
    if w.file_format == "pdf":
        pdf = w.file_path
        if not (pdf and os.path.exists(pdf)):
            try:
                from backend.calibre import sync as csync

                pdf = csync.ensure_cached(w.id)
            except Exception:  # noqa: BLE001
                pdf = None
        if pdf and os.path.exists(pdf):
            c = covers.extract_pdf_cover(pdf, sha)
            if c:
                return c, "pdf"
    return None, ""


def main(ids: list[int] | None) -> None:
    with Session(engine) as s:
        works = s.exec(select(Work)).all()
        if ids:
            works = [w for w in works if w.id in ids]
        else:
            works = [w for w in works if _needs_cover(w)]
        print(f"перепоиск обложек: {len(works)} книг", flush=True)
        found = 0
        for i, w in enumerate(works, 1):
            try:
                c, src = _real_cover(w)
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(works)}] {w.id} {w.title!r}: ERROR {e}", flush=True)
                continue
            if c:
                old = w.cover_path
                w.cover_path = str(c)
                w.cover_source = src
                s.add(w)
                s.commit()
                if old and old != str(c) and os.path.exists(old):
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                found += 1
                print(f"[{i}/{len(works)}] ✓ {w.id} {w.title!r} -> {src}", flush=True)
        print(f"ГОТОВО: заменено {found}/{len(works)}", flush=True)


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1:]] or None)
