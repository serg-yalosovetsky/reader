"""Чтение и пере-сборка бэкапа ReadEra (`.bak` = zip с library.json).

Схема (ReadEra Premium, db v110):
- library.json = {"docs": [...], "colls": [...], "words": [...]}
- doc = {"uri": "sha-1:<hex>", "data": {...}, "citations": [...], ...}
- data.doc_sha1            — SHA-1 файла (идентификатор книги, = наш Work.sha1)
- data.doc_position        — JSON-СТРОКА: {"ratio":0..1,"page":N,"pagesCount":M,
                             "offsetY":..,"xPath":"..","version":2,...}
- data.doc_last_read_time  — epoch ms (для last-write-wins)
- citations[]             — закладки/цитаты

Импорт (чтение прогресса) надёжен. Запись (patch) — best-effort: меняем ratio,
page и last_read_time; xPath оставляем (ReadEra может предпочесть его при restore).
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

LIBRARY_ENTRY = "library.json"


@dataclass
class ReadEraDoc:
    sha1: str
    title: str
    ratio: float
    last_read_time: int  # epoch ms
    citations: list[dict] = field(default_factory=list)


def read_backup(bak_path: str | Path) -> dict[str, ReadEraDoc]:
    """Распарсить .bak → {sha1: ReadEraDoc} только для книг с известным sha1."""
    with zipfile.ZipFile(bak_path) as z:
        raw = z.read(LIBRARY_ENTRY)
    lib = json.loads(raw.decode("utf-8"))
    out: dict[str, ReadEraDoc] = {}
    for d in lib.get("docs", []):
        data = d.get("data", {})
        sha1 = data.get("doc_sha1")
        if not sha1:
            continue
        ratio = _ratio_of(data.get("doc_position"))
        out[sha1] = ReadEraDoc(
            sha1=sha1,
            title=data.get("doc_title", ""),
            ratio=ratio,
            last_read_time=int(data.get("doc_last_read_time") or 0),
            citations=d.get("citations", []) or [],
        )
    return out


def _ratio_of(position) -> float:
    """Достать ratio из doc_position (строка JSON или dict)."""
    if not position:
        return 0.0
    try:
        obj = json.loads(position) if isinstance(position, str) else position
        return float(obj.get("ratio", 0.0) or 0.0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def patch_backup(
    src_bak: str | Path,
    dst_bak: str | Path,
    updates: dict[str, tuple[float, int]],
) -> Path:
    """Собрать новый .bak из исходного, обновив позиции для sha1 из `updates`.

    updates: {sha1: (ratio, last_read_time_ms)}.
    Все прочие записи (meta.json, prefs.xml, ...) переносятся без изменений.
    Возвращает путь dst_bak.
    """
    src_bak, dst_bak = Path(src_bak), Path(dst_bak)
    with zipfile.ZipFile(src_bak) as z:
        names = z.namelist()
        lib = json.loads(z.read(LIBRARY_ENTRY).decode("utf-8"))
        others = {n: z.read(n) for n in names if n != LIBRARY_ENTRY}

    patched = 0
    for d in lib.get("docs", []):
        data = d.get("data", {})
        sha1 = data.get("doc_sha1")
        if sha1 in updates:
            ratio, lrt = updates[sha1]
            data["doc_position"] = _set_position(data.get("doc_position"), ratio)
            data["doc_last_read_time"] = int(lrt)
            data["doc_activity_time"] = int(lrt)
            patched += 1

    new_lib = json.dumps(lib, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(dst_bak, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(LIBRARY_ENTRY, new_lib)
        for n, blob in others.items():
            z.writestr(n, blob)
    return dst_bak


def _set_position(position, ratio: float) -> str:
    """Обновить ratio (и согласовать page/offset) в doc_position-строке."""
    try:
        obj = json.loads(position) if isinstance(position, str) else (position or {})
    except (json.JSONDecodeError, TypeError):
        obj = {}
    obj["ratio"] = float(ratio)
    pages = obj.get("pagesCount") or 0
    if pages:
        obj["page"] = max(0, min(int(pages) - 1, round(ratio * pages)))
    obj["offsetX"] = 0
    obj["offsetY"] = 0
    # xPath оставляем как есть — пересчитать его без книги нельзя; ReadEra при
    # расхождении обычно опирается на ratio/page. Это best-effort запись.
    return json.dumps(obj, ensure_ascii=False)
