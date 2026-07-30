"""Вернуть на место Work-ссылки Calibre, которые синк «поднял наверх».

Проблема: sync_catalog ставил новым Work-ссылкам updated_at=utcnow(), а библиотека
сортируется по updated_at DESC. Книга, годами лежащая в каталоге Calibre, в момент
первого синка оказывалась в самом верху, вытесняя то, что реально читают.

Чиним только записи, где всплытие — артефакт синка: site=calibre, файл не качали,
прогресса нет, updated_at == created_at (±2 c, т.е. с тех пор ничего не менялось).
Новое значение — дата из каталога Calibre (OPDS <updated>), нет её — 1970.

Запуск:  cd /root/reader && set -a; . ./.env; set +a; \\
         .venv/bin/python scripts/backfill_calibre_updated.py [--apply]
"""
from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, "/root/reader")

from sqlmodel import Session, select  # noqa: E402

from backend.app.db.models import Progress, Work  # noqa: E402
from backend.app.db.session import engine  # noqa: E402
from backend.calibre import client  # noqa: E402

APPLY = "--apply" in sys.argv
EPOCH = datetime(1970, 1, 1)

catalog = {b["calibre_id"]: b.get("updated") for b in client.list_books()}
print(f"каталог Calibre: {len(catalog)} книг, с датой: "
      f"{sum(1 for v in catalog.values() if v)}")

with Session(engine) as s:
    with_progress = {
        p.work_id for p in s.exec(select(Progress)).all()
    }
    rows = s.exec(select(Work).where(Work.site == "calibre")).all()
    touched = 0
    samples = []
    rollback = []
    for w in rows:
        if w.file_path or w.id in with_progress:
            continue
        delta = abs((w.updated_at - w.created_at).total_seconds())
        if delta > 2:
            continue  # запись жила своей жизнью — не трогаем
        new = catalog.get(w.calibre_id) or EPOCH
        if abs((w.updated_at - new).total_seconds()) < 2:
            continue  # уже корректна
        if len(samples) < 10:
            samples.append(
                f"  {w.id:5d} {w.title[:44]:44s} {w.updated_at:%Y-%m-%d %H:%M} -> {new:%Y-%m-%d %H:%M}"
            )
        rollback.append((w.id, w.updated_at))
        w.updated_at = new
        s.add(w)
        touched += 1
    print(f"\nзатронуто записей: {touched}")
    print("\n".join(samples))
    if APPLY:
        # Откатный дамп: id,старый updated_at — перед записью.
        import csv
        with open('/root/reader/data/backfill_updated_at.rollback.csv', 'w', newline='') as f:
            wr = csv.writer(f)
            wr.writerow(['work_id', 'old_updated_at'])
            for wid, old_dt in rollback:
                wr.writerow([wid, old_dt.isoformat()])
        print('откатный дамп: data/backfill_updated_at.rollback.csv')
        s.commit()
        print("\nЗАПИСАНО")
    else:
        s.rollback()
        print("\nDRY-RUN (запуск с --apply, чтобы применить)")
