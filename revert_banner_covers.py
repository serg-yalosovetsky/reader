"""Одноразовый откат: у книг, чья «обложка» = дженерик-баннер источника, сбросить
cover_path/cover_source, чтобы они снова ушли в ИИ-генерацию. Опц. --warm — сразу
прогреть генерацию через локальный API. Запуск: cd /root/reader && set -a; . .env;
set +a; .venv/bin/python revert_banner_covers.py [--warm]"""

from __future__ import annotations

import os
import sys

from sqlmodel import Session, select

from backend.app.covers import is_generic_cover
from backend.app.db.models import Work
from backend.app.db.session import engine


def main() -> None:
    warm = "--warm" in sys.argv
    cleared: list[int] = []
    removed_files: list[str] = []
    with Session(engine) as s:
        for w in s.exec(select(Work)).all():
            p = w.cover_path
            if not p or not os.path.exists(p):
                continue
            try:
                data = open(p, "rb").read()
            except OSError:
                continue
            if not is_generic_cover(data):
                continue
            # это баннер-заглушка → сбросить, чтобы /cover запланировал ИИ-генерацию
            w.cover_path = ""
            w.cover_source = ""  # НЕ gen_failed — иначе генерация не повторится
            s.add(w)
            cleared.append(w.id)
            removed_files.append(p)
        s.commit()

    for p in removed_files:
        try:
            os.remove(p)
        except OSError:
            pass

    print(f"cleared={len(cleared)} ids={cleared}")
    print(f"files_removed={len(removed_files)}")

    if warm and cleared:
        import time
        import urllib.request

        base = "http://127.0.0.1:8123"
        for wid in cleared:
            url = f"{base}/api/reader/{wid}/cover/generate?force=1"
            try:
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(req, timeout=180) as r:
                    print(f"warm {wid}: {r.status}")
            except Exception as e:  # noqa: BLE001
                print(f"warm {wid}: FAIL {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
