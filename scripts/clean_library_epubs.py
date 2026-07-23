"""Ретро-чистка уже скачанных EPUB: мусор readli/AT внутри библиотеки.

Парсер (backend/downloaders/textclean.py) чинит только НОВЫЕ загрузки —
книги, скачанные раньше, продолжают показывать «quoter = 0;», рекламные
пустышки, слипшиеся заголовки глав («Глава перваяЭскадрон») и промо-хвост
author.today. Этот скрипт правит уже лежащие файлы на месте.

Использование (из /root/reader, venv):
    .venv/bin/python scripts/clean_library_epubs.py --dry-run          # весь data/books
    .venv/bin/python scripts/clean_library_epubs.py --file <path.epub> # одна книга
    .venv/bin/python scripts/clean_library_epubs.py --apply            # применить

С --apply рядом кладётся <файл>.bak (если бэкапа ещё нет).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.downloaders.textclean import (  # noqa: E402
    _PROMO_TAIL_RE,
    _QUOTER_TEXT_RE,
    clean_title,
)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# Рекламные пустышки, доехавшие в EPUB: <div caramel-id="…"/>, dc-feed, yandex_rtb.
_AD_DIV_RE = re.compile(
    r"<div\b[^>]*\b(?:caramel-id|dc-feed|yandex_rtb)[^>]*?(?:/>|>\s*</div>)",
    re.I | re.S,
)
_HEADING_RE = re.compile(r"(<(h[1-4]|title)\b[^>]*>)(.*?)(</\2>)", re.S | re.I)
_NCX_LABEL_RE = re.compile(r"(<text>)(.*?)(</text>)", re.S)
_DC_TITLE_RE = re.compile(r"(<dc:title[^>]*>)(.*?)(</dc:title>)", re.S)
_WS_RE = re.compile(rb"\s+")


def _squash(blob: bytes) -> bytes:
    return _WS_RE.sub(b" ", blob)


def _clean_doc(text: str) -> str:
    """XHTML главы: убрать мусор в теле + починить заголовок в <title>/<hN>."""
    out = _COMMENT_RE.sub("", text)
    out = _AD_DIV_RE.sub("", out)
    out = _PROMO_TAIL_RE.sub("", out)
    out = _QUOTER_TEXT_RE.sub("", out)

    def _fix(m: re.Match) -> str:
        inner = m.group(3)
        if "<" in inner:  # разметка внутри заголовка — не трогаем
            return m.group(0)
        return m.group(1) + (clean_title(inner) or "") + m.group(4)

    return _HEADING_RE.sub(_fix, out)


def _clean_ncx(text: str) -> str:
    """Оглавление: те же слипшиеся заголовки в <navLabel><text>."""

    def _fix(m: re.Match) -> str:
        return m.group(1) + (clean_title(m.group(2)) or "") + m.group(3)

    return _NCX_LABEL_RE.sub(_fix, text)


def _clean_opf(text: str) -> str:
    def _fix(m: re.Match) -> str:
        return m.group(1) + (clean_title(m.group(2)) or "") + m.group(3)

    return _DC_TITLE_RE.sub(_fix, text)


def clean_epub(path: Path, apply: bool) -> tuple[int, list[str]]:
    """Вернуть (число изменённых внутренних файлов, их имена). apply=False — только счёт."""
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]

    changed: list[str] = []
    new_items: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, blob in items:
        name = info.filename.lower()
        out = blob
        if name.endswith((".xhtml", ".html", ".htm")):
            text = blob.decode("utf-8", "replace")
            fixed = _clean_doc(text)
            out = fixed.encode("utf-8")
        elif name.endswith(".ncx"):
            out = _clean_ncx(blob.decode("utf-8", "replace")).encode("utf-8")
        elif name.endswith(".opf"):
            out = _clean_opf(blob.decode("utf-8", "replace")).encode("utf-8")
        # Разница только в пробелах (clean_title схлопывает отступы в <text>/<title>)
        # — не повод перезаписывать книгу.
        if out != blob and _squash(out) != _squash(blob):
            changed.append(info.filename)
        else:
            out = blob
        new_items.append((info, out))

    if changed and apply:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(path, bak)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            # mimetype обязан быть первым и без сжатия (спека EPUB).
            for info, blob in new_items:
                if info.filename == "mimetype":
                    z.writestr(info, blob, compress_type=zipfile.ZIP_STORED)
            for info, blob in new_items:
                if info.filename != "mimetype":
                    z.writestr(info, blob)
        tmp.replace(path)
    return len(changed), changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="один .epub")
    ap.add_argument("--dir", default="data/books", help="каталог библиотеки")
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    args = ap.parse_args()

    files = (
        [Path(args.file)]
        if args.file
        else sorted(Path(args.dir).glob("*.epub"))
    )
    total_books = 0
    for f in files:
        try:
            n, names = clean_epub(f, args.apply)
        except zipfile.BadZipFile:
            print(f"!! не zip: {f}")
            continue
        if n:
            total_books += 1
            print(f"{'FIX ' if args.apply else 'DRY '}{f.name}: {n} файл(ов) — {names[:4]}")
    print(f"\nитого книг с мусором: {total_books} из {len(files)}")
    if not args.apply:
        print("это dry-run; для записи добавь --apply")


if __name__ == "__main__":
    main()
