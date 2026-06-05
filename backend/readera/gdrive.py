"""Доступ к бэкапам ReadEra в Google Drive через rclone (настроен на VPS:
remote `gdrive:`, /root/.config/rclone/rclone.conf, scope drive).

Все функции — тонкие обёртки над rclone CLI; на машинах без rclone/без remote
они мягко возвращают пусто/False (для локальной разработки).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..app.config import RCLONE_BIN, READERA_BACKUP_REMOTE, READERA_BOOKS_REMOTE


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [RCLONE_BIN, *args], capture_output=True, text=True, timeout=timeout
    )


def available() -> bool:
    """rclone есть и папка бэкапов задана."""
    if not READERA_BACKUP_REMOTE:
        return False
    try:
        return _run(["version"], timeout=15).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def latest_backup() -> dict | None:
    """Найти самый свежий *.bak в папке бэкапов. Возвращает {name, path, modtime}."""
    if not READERA_BACKUP_REMOTE:
        return None
    try:
        p = _run(["lsjson", READERA_BACKUP_REMOTE], timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        items = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return None
    baks = [
        it for it in items
        if not it.get("IsDir") and str(it.get("Name", "")).lower().endswith(".bak")
    ]
    if not baks:
        return None
    newest = max(baks, key=lambda it: it.get("ModTime", ""))
    return {
        "name": newest["Name"],
        "path": f"{READERA_BACKUP_REMOTE}/{newest['Name']}",
        "modtime": newest.get("ModTime", ""),
    }


def pull(remote_path: str, local_path: str | Path) -> bool:
    """Скачать файл из Drive в local_path."""
    try:
        return _run(["copyto", remote_path, str(local_path)], timeout=300).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def push(local_path: str | Path, remote_path: str) -> bool:
    """Залить локальный файл в Drive по remote_path."""
    try:
        return _run(["copyto", str(local_path), remote_path], timeout=300).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def push_book(local_file: str | Path) -> bool:
    """Положить книгу в папку ReadEra/Books (ReadEra Premium подхватит авто-синком).
    Без READERA_BOOKS_REMOTE — no-op."""
    if not READERA_BOOKS_REMOTE:
        return False
    name = Path(local_file).name
    return push(local_file, f"{READERA_BOOKS_REMOTE}/{name}")
