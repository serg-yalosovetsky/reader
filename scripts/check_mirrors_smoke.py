"""Диагностика поиска зеркал (monitor._check_mirrors): без БД-записи и без скачивания.

Проверяет, что _check_mirrors не падает и укладывается по времени на реальных
книгах разных хостов. Ничего не мутирует.
"""
import sys
import time

sys.path.insert(0, "/root/reader")

from sqlmodel import Session, select  # noqa: E402

from backend.accounts import monitor  # noqa: E402
from backend.app.db.models import Monitored, Work  # noqa: E402
from backend.app.db.session import engine  # noqa: E402

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10

with Session(engine) as s:
    rows = []
    for mon in s.exec(select(Monitored).order_by(Monitored.id)).all():
        if not mon.work_id or not mon.source_url:
            continue
        w = s.get(Work, mon.work_id)
        if not w or not w.title:
            continue
        rows.append((mon.id, monitor._host(mon.source_url), w.title, monitor._descriptor(w)))

print(f"подписок с опознанной книгой: {len(rows)}; пробуем первые {LIMIT}\n")

t0 = time.time()
ok = fail = found = 0
for mid, host, title, our in rows[:LIMIT]:
    t = time.time()
    try:
        res = monitor._check_mirrors(our)
        ok += 1
        if res:
            found += 1
        print(f"  mon {mid:<4} [{host:<16}] {title[:34]:<34} -> {res} ({time.time()-t:.1f}s)")
    except Exception as e:  # noqa: BLE001
        fail += 1
        print(f"  mon {mid:<4} [{host:<16}] {title[:34]:<34} -> ИСКЛЮЧЕНИЕ {type(e).__name__}: {e}")

n = min(LIMIT, len(rows))
print(f"\nитог: ok={ok} exceptions={fail} зеркал найдено={found}")
print(f"время: {time.time()-t0:.1f}s на {n} подписок "
      f"({(time.time()-t0)/max(n,1):.1f}s на подписку, в check_all пул из 5)")
