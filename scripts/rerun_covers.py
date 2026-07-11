"""Перепоиск настоящих обложек для книг с ИИ-обложкой (cover_source generated/
gen_failed). Для каждой пробуем встроенную в файл + источник/зеркала (ficbook,
author.today, searchfloor). Нашли настоящую → заменяем ИИ и удаляем старый файл.

Запуск (с загруженным .env → Postgres!):
    set -a; . ./.env; set +a
    .venv/bin/python scripts/rerun_covers.py [work_id ...]   # без id — все ИИ-книги
"""

from __future__ import annotations

import os
import sys

from sqlmodel import Session, select

from backend.app import covers
from backend.app.db.models import Work
from backend.app.db.session import engine


def _real_cover(w: Work):
    # 1) встроенная в файл книги
    if w.file_path and os.path.exists(w.file_path):
        c = covers.extract_cover(w.file_path, w.file_format, w.sha1)
        if c:
            return c, "embedded"
    # 2) источник + зеркала (ficbook/author.today/searchfloor) по title/author
    sha = w.sha1 or (f"cal{w.calibre_id}" if w.calibre_id else f"w{w.id}")
    c = covers.fetch_source_cover(w.source_url or "", sha, w.title, w.author)
    if c:
        return c, "source"
    return None, ""


def main(ids: list[int] | None) -> None:
    with Session(engine) as s:
        works = s.exec(
            select(Work).where(Work.cover_source.in_(["generated", "gen_failed"]))
        ).all()
        if ids:
            works = [w for w in works if w.id in ids]
        print(f"pererun covers: {len(works)} книг", flush=True)
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
            else:
                print(
                    f"[{i}/{len(works)}] – {w.id} {w.title!r}: нет обложки", flush=True
                )
        print(f"ГОТОВО: заменено {found}/{len(works)}", flush=True)


if __name__ == "__main__":
    arg_ids = [int(x) for x in sys.argv[1:]] or None
    main(arg_ids)
