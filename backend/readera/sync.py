"""Двусторонняя синхронизация прогресса с ReadEra через её ручной бэкап (.bak).

Матч книг по SHA-1 файла (ReadEra `doc_sha1` == наш `Work.sha1`), реконсиляция
last-write-wins по времени последнего чтения.

- import: свежий .bak из Drive → обновляем наш Progress там, где ReadEra новее.
- export: где веб-прогресс новее — патчим .bak и кладём в Drive отдельным файлом
  `ReadEra-restore-*.bak` (пользователь делает Restore в ReadEra; авто-вливания нет).
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from ..app.config import READERA_BACKUP_REMOTE
from ..app.db.models import Progress, Work, utcnow
from . import backup, gdrive


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _pull_latest() -> Path | None:
    """Скачать самый свежий бэкап ReadEra во временный файл."""
    newest = gdrive.latest_backup()
    if not newest:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="readera_")) / newest["name"]
    return tmp if gdrive.pull(newest["path"], tmp) else None


def import_progress(session: Session, bak_path: str | Path | None = None) -> dict:
    """ReadEra → читалка. bak_path можно передать напрямую (ручная загрузка)."""
    path = Path(bak_path) if bak_path else _pull_latest()
    if not path or not path.exists():
        return {"ok": False, "reason": "нет доступного бэкапа ReadEra", "updated": 0}

    docs = backup.read_backup(path)
    works = session.exec(select(Work).where(Work.sha1 != "")).all()
    updated = 0
    for w in works:
        redoc = docs.get(w.sha1)
        if not redoc or redoc.ratio <= 0:
            continue
        prog = session.exec(select(Progress).where(Progress.work_id == w.id)).first()
        our_ms = _ms(prog.last_read_time) if prog else 0
        if redoc.last_read_time <= our_ms:
            continue
        if prog:
            prog.ratio = redoc.ratio
            prog.locator = ""  # позиция из ReadEra — по ratio (CFI нет)
            prog.last_read_time = _from_ms(redoc.last_read_time)
            prog.source = "readera"
        else:
            prog = Progress(
                work_id=w.id, ratio=redoc.ratio, locator="",
                last_read_time=_from_ms(redoc.last_read_time), source="readera",
            )
            session.add(prog)
        updated += 1
    session.commit()
    return {"ok": True, "updated": updated, "readera_docs": len(docs)}


def export_progress(session: Session) -> dict:
    """Читалка → ReadEra: патчим свежий .bak позициями, где веб новее, и кладём
    в Drive как ReadEra-restore-<ts>.bak. Возвращает имя файла для restore."""
    path = _pull_latest()
    if not path:
        return {"ok": False, "reason": "нет доступного бэкапа ReadEra для патча", "patched": 0}

    docs = backup.read_backup(path)
    updates: dict[str, tuple[float, int]] = {}
    works = session.exec(select(Work).where(Work.sha1 != "")).all()
    for w in works:
        redoc = docs.get(w.sha1)
        if not redoc:
            continue
        prog = session.exec(select(Progress).where(Progress.work_id == w.id)).first()
        if not prog or prog.ratio <= 0:
            continue
        our_ms = _ms(prog.last_read_time)
        if our_ms > redoc.last_read_time and abs(prog.ratio - redoc.ratio) > 1e-3:
            updates[w.sha1] = (prog.ratio, our_ms)

    if not updates:
        return {"ok": True, "patched": 0, "reason": "нечего экспортировать"}

    stamp = _ms(utcnow())
    out = path.with_name(f"ReadEra-restore-{stamp}.bak")
    backup.patch_backup(path, out, updates)
    remote = f"{READERA_BACKUP_REMOTE}/{out.name}"
    pushed = gdrive.push(out, remote)
    return {
        "ok": pushed, "patched": len(updates),
        "restore_file": out.name if pushed else None,
        "hint": "в ReadEra: Настройки → Восстановить из файла → выбрать этот .bak",
    }


def sync(session: Session) -> dict:
    """Полная синхронизация: импорт, затем экспорт."""
    imp = import_progress(session)
    exp = export_progress(session)
    return {"import": imp, "export": exp}
