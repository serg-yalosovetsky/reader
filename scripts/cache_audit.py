"""Аудит кэша Calibre: что в нём лежит и читалось ли это когда-нибудь.

Кэш вытесняемый, но конечный. Если он занят книгами, которые ни разу не
открывали, то книги, которые действительно читают, вытесняются — и каждое их
открытие снова тянет файл с роутера. Скрипт только СМОТРИТ, ничего не удаляет.

Запуск: cd /root/reader && set -a; . ./.env; set +a && .venv/bin/python scripts/cache_audit.py
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from backend.app.config import CALIBRE_CACHE_DIR, CALIBRE_CACHE_MAX_MB
from backend.app.db.models import Progress, Work
from backend.app.db.session import engine

MB = 1024 * 1024


def main() -> None:
    files = sorted(
        (p for p in CALIBRE_CACHE_DIR.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    total = sum(p.stat().st_size for p in files)
    print(f"Кэш: {CALIBRE_CACHE_DIR}")
    print(f"Занято {total / MB:.0f} МБ из {CALIBRE_CACHE_MAX_MB} МБ, файлов {len(files)}")
    print(f"Свободно до вытеснения: {CALIBRE_CACHE_MAX_MB - total / MB:.0f} МБ\n")

    never_read_bytes = 0
    never_read = 0
    read_bytes = 0
    read_n = 0
    orphan_bytes = 0
    orphan = 0
    converted_ready = 0

    with Session(engine) as s:
        for p in files:
            size = p.stat().st_size
            try:
                cid = int(p.stem)
            except ValueError:
                orphan += 1
                orphan_bytes += size
                continue
            work = s.exec(select(Work).where(Work.calibre_id == cid)).first()
            if not work:
                orphan += 1
                orphan_bytes += size
                continue
            if work.converted_status == "ready":
                converted_ready += 1
            prog = s.exec(select(Progress).where(Progress.work_id == work.id)).first()
            if prog:
                read_n += 1
                read_bytes += size
            else:
                never_read += 1
                never_read_bytes += size

    print(f"Открывались (есть прогресс): {read_n:>4} шт, {read_bytes / MB:>7.0f} МБ")
    print(f"НИ РАЗУ не открывались:      {never_read:>4} шт, {never_read_bytes / MB:>7.0f} МБ")
    print(f"Нет книги в БД (сироты):     {orphan:>4} шт, {orphan_bytes / MB:>7.0f} МБ")
    print(f"\nС готовой EPUB-версией: {converted_ready} шт "
          f"(только они уходят из кэша первыми при переполнении)")


if __name__ == "__main__":
    main()
