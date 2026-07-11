"""Извлечь обложку из первой страницы PDF для Calibre-книг с ИИ/плейсхолдер-
обложкой (у которых Calibre не отдал реальную). Запуск с загруженным .env.

    .venv/bin/python scripts/fix_pdf_covers.py [work_id ...]   # без id — все PDF без реальной обложки
"""

from __future__ import annotations

import os
import sys

from sqlmodel import Session, select

from backend.app import covers
from backend.app.db.models import Work
from backend.app.db.session import engine
from backend.calibre import sync as csync


def _fix_one(s: Session, w: Work) -> bool:
    pdf = csync.ensure_cached(w.id)
    if not pdf or not os.path.exists(pdf):
        print(f"  [{w.id}] {w.title!r}: PDF не скачался", flush=True)
        return False
    sha = w.sha1 or (f"cal{w.calibre_id}" if w.calibre_id else f"w{w.id}")
    p = covers.extract_pdf_cover(pdf, sha)
    if not p:
        print(f"  [{w.id}] {w.title!r}: обложка из PDF не извлеклась", flush=True)
        return False
    old = w.cover_path
    w.cover_path = str(p)
    w.cover_source = "pdf"
    s.add(w)
    s.commit()
    if old and old != str(p) and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    print(f"  [{w.id}] ✓ {w.title!r} -> {covers._img_size(p.read_bytes())}", flush=True)
    return True


def main(ids: list[int] | None) -> None:
    with Session(engine) as s:
        if ids:
            works = [s.get(Work, i) for i in ids]
            works = [w for w in works if w]
        else:
            works = [
                w
                for w in s.exec(select(Work).where(Work.file_format == "pdf")).all()
                if w.cover_source in ("generated", "gen_failed", "calibre", "")
            ]
        print(f"PDF-обложки: {len(works)} книг", flush=True)
        n = 0
        for w in works:
            try:
                if _fix_one(s, w):
                    n += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [{w.id}] ERROR {e}", flush=True)
        print(f"ГОТОВО: {n}/{len(works)}", flush=True)


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1:]] or None)
