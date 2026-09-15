"""Починка книг author.today с мусорными абзацами после фикса расшифровки (serg/tasks#899).

До коммита c937f17 _decrypt шёл по code point'ам Python, а author.today шифрует по
UTF-16-юнитам: текст после каждого эмодзи превращался в мусор. Обычная перекачка
такие книги НЕ чинит: правильный текст короче мусорного, и register_download
(замена только «полнее или больше глав») оставляет битый файл.

Скрипт перекачивает указанные книги с их author.today-источника уже исправленным
кодом и заменяет файл ТОЛЬКО если в новом нет мусорных абзацев и глав не меньше.
Старый файл остаётся на диске (имя по sha1), снимок строк — в backups/.

Запуск (из /root/reader, с окружением сервиса — нужен READER_DB_URL):
  .venv/bin/python scripts/repair_at_garbled_tails.py 3406 46 1489          # всухую
  .venv/bin/python scripts/repair_at_garbled_tails.py 3406 46 1489 --apply  # заменить
Запускать только ПОСЛЕ деплоя фикса: иначе монитор на старом коде перекачает главы
с мусором, и «более полный» мусорный файл снова вытеснит исправленный.
"""

from __future__ import annotations

import html
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session  # noqa: E402

from backend.app.db.models import Work, utcnow  # noqa: E402
from backend.app.db.session import engine  # noqa: E402
from backend.app.services import _apply_file, count_sections  # noqa: E402
from backend.app.storage import import_file, sha1_of_file  # noqa: E402
from backend.downloaders import authortoday  # noqa: E402


# Эмодзи и пиктограммы живут вне BMP и в правильно расшифрованном тексте встречаются
# (авторские приписки «👽👽👽», «🤝🤱»). Сбой расшифровки же даёт символы из других
# плоскостей: клинопись U+12xxx, иероглифы U+13xxx, CJK Ext B U+2xxxx, плюс U+FFFD и
# управляющие символы. Первая версия считала мусором любой символ вне BMP и отвергала
# уже исправленные файлы (живой прогон 2026-09-15).
_EMOJI_PLANE = range(0x1F000, 0x1FB00)
_REPLACEMENT = chr(0xFFFD)


def _is_garbage(text: str) -> bool:
    for c in text:
        o = ord(c)
        if c == _REPLACEMENT or (o < 0x20 and c not in (chr(9), chr(10), chr(13))):
            return True
        if o > 0xFFFF and o not in _EMOJI_PLANE:
            return True
    return False


def garbled_chapters(path: str | Path) -> int:
    """Сколько глав EPUB содержат абзацы с символами-признаками сбоя расшифровки."""
    z = zipfile.ZipFile(path)
    n = 0
    for name in z.namelist():
        if not name.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        doc = z.read(name).decode("utf-8", "replace")
        paras = re.findall(r"<p[^>]*>(.*?)</p>", doc, re.S)
        if any(_is_garbage(html.unescape(re.sub(r"<[^>]+>", "", p))) for p in paras):
            n += 1
    return n


def main() -> int:
    apply = "--apply" in sys.argv
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    if not ids:
        print(__doc__)
        return 2
    changed: list[dict] = []
    failures = 0
    for wid in ids:
        with Session(engine) as s:
            w = s.get(Work, wid)
            if w is None:
                print(f"work {wid}: нет в библиотеке")
                failures += 1
                continue
            snap = {
                "id": w.id, "title": w.title, "site": w.site, "source_url": w.source_url,
                "file_path": w.file_path, "file_format": w.file_format, "sha1": w.sha1,
                "chapters_count": w.chapters_count,
            }
        old_bad = garbled_chapters(snap["file_path"]) if snap["file_path"] else 0
        old_ch = count_sections(snap["file_path"], snap["file_format"], book_title=snap["title"] or "")
        print(f"work {wid} «{snap['title']}»: глав {old_ch}, с мусором {old_bad}, источник {snap['source_url']}")
        if "author.today/work/" not in (snap["source_url"] or ""):
            print("  пропуск: источник не author.today")
            failures += 1
            continue
        try:
            res = authortoday.download(snap["source_url"])
        except Exception as e:  # noqa: BLE001 — одна книга не должна валить остальные; причина печатается
            print(f"  скачивание не удалось: {type(e).__name__}: {e}")
            failures += 1
            continue
        new_bad = garbled_chapters(res.file_path)
        new_ch = count_sections(res.file_path, res.file_format, book_title=res.title or "")
        print(f"  новый файл: глав {new_ch}, с мусором {new_bad}")
        if new_bad or new_ch < old_ch:
            print("  отказ: новый файл не лучше (есть мусор или глав меньше) — старый не трогаю")
            failures += 1
            continue
        if not apply:
            print("  всухую: заменил бы файл")
            continue
        sha1 = sha1_of_file(res.file_path)
        dest, _ = import_file(res.file_path, sha1)
        with Session(engine) as s:
            w = s.get(Work, wid)
            _apply_file(w, dest, res, sha1)
            w.updated_at = utcnow()
            s.add(w)
            s.commit()
            s.refresh(w)
            after = {"file_path": w.file_path, "sha1": w.sha1, "chapters_count": w.chapters_count}
        print(f"  заменён: {snap['sha1'][:10]} → {after['sha1'][:10]}, глав {after['chapters_count']}")
        changed.append({"before": snap, "after": after})
    if changed:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(__file__).resolve().parents[1] / "backups" / f"at-garbled-repair-{stamp}.json"
        out.write_text(json.dumps(changed, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"снимок до/после: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
