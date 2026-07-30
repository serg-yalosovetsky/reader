"""Две разовые правки данных ридера.

1. Ложный бейдж «Calibre». migrate_local_to_refs() ставил site="calibre" книгам,
   которые мы качаем сами; часть из них потом снова обзавелась собственным файлом
   (монитор докачал). UI рисует бейдж по site → фанфик с ficbook/author.today
   выглядит как книга из Calibre. Возвращаем site по домену source_url тем, у кого
   есть СВОЙ файл. calibre_id не трогаем — связь с каталогом остаётся.

2. Разбор аварии с «Вечно голодный студент 9» (work 353): фоллбэк на зеркала
   притащил соседний том серии (readli «...студент 5», work 2133) и перевёл на него
   подписку 155. Возвращаем подписку на 353, ошибочный Work удаляем.

Запуск: cd /root/reader && set -a; . ./.env; set +a; \\
        .venv/bin/python scripts/fix_false_calibre_and_vgs9.py [--apply]
"""
from __future__ import annotations

import sys
from urllib.parse import urlparse

sys.path.insert(0, "/root/reader")

from sqlmodel import Session, select  # noqa: E402

from backend.app.db.models import Monitored, Progress, Work  # noqa: E402
from backend.app.db.session import engine  # noqa: E402

APPLY = "--apply" in sys.argv

HOST_TO_SITE = {
    "ficbook.net": "ficbook",
    "author.today": "authortoday",
    "readli.net": "readli",
    "searchfloor.org": "searchfloor",
    "fanfics.me": "fanfics",
    "archiveofourown.org": "ao3",
    "www.fanfiction.net": "ffn",
    "fanfiction.net": "ffn",
}

with Session(engine) as s:
    # ---------------------------------------------------------- 1) site
    fixed = 0
    for w in s.exec(select(Work).where(Work.site == "calibre")).all():
        if not w.file_path or not w.source_url:
            continue  # настоящая calibre-ссылка (файл живёт в Calibre) — не трогаем
        host = (urlparse(w.source_url).hostname or "").lower()
        site = HOST_TO_SITE.get(host)
        if not site:
            continue
        if fixed < 8:
            print(f"  {w.id:5d} {w.title[:40]:40s} calibre -> {site}")
        w.site = site
        s.add(w)
        fixed += 1
    print(f"site исправлен у {fixed} книг")

    # ------------------------------------------------- 2) ВГС-9 / том 5
    mon = s.get(Monitored, 155)
    bad = s.get(Work, 2133)
    print("\nподписка 155:", None if not mon else f"work_id={mon.work_id} src={mon.source_url}")
    print("work 2133:", None if not bad else f"{bad.title!r} site={bad.site} src={bad.source_url}")
    if mon and mon.work_id == 2133:
        mon.work_id = 353
        # Источник — бесплатное зеркало readli (на author.today хвост платный,
        # сервер отвечает Paid и книга стоит с 20 главами).
        mon.source_url = "https://readli.net/chitat-online/?b=1382688"
        mon.last_seen_chapters = 0  # пересчитать при следующей проверке
        mon.fail_count = 0
        mon.last_error = None
        s.add(mon)
        print("  -> подписка возвращена на work 353, источник = readli b=1382688")
    if bad and bad.id == 2133:
        if s.exec(select(Progress).where(Progress.work_id == 2133)).first():
            print("  !! у 2133 есть прогресс — НЕ удаляю")
        else:
            s.delete(bad)
            print("  -> ошибочный Work 2133 («…студент 5») удалён")

    if APPLY:
        s.commit()
        print("\nЗАПИСАНО")
    else:
        s.rollback()
        print("\nDRY-RUN (--apply чтобы применить)")
