"""Общий сборщик EPUB из HTML-секций (для адаптеров readli/searchfloor и др.).

Только NCX-оглавление (EpubNav в ebooklib падает на page-list при служебных
документах). Тело секции всегда непустое.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ebooklib import epub


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_epub(
    identifier: str,
    title: str,
    author: str,
    sections: list[tuple[str | None, str]],
    *,
    lang: str = "ru",
    annotation: str = "",
    cover: bytes | None = None,
) -> Path:
    """sections: список (заголовок|None, html). cover — байты обложки (встраивается).
    Возвращает путь к .epub."""
    book = epub.EpubBook()
    book.set_identifier(identifier)
    book.set_title(title or "Без названия")
    book.set_language(lang)
    if author:
        book.add_author(author)
    if annotation:
        book.add_metadata("DC", "description", annotation)
    if cover:
        ext = "png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
        try:
            book.set_cover(f"cover.{ext}", cover)
        except Exception:  # noqa: BLE001 — обложка не критична для сборки
            pass

    spine: list = []
    toc: list = []
    for i, (heading, html) in enumerate(sections, 1):
        item = epub.EpubHtml(title=heading or f"Часть {i}", file_name=f"part_{i}.xhtml", lang=lang)
        body = (html or "").strip() or "<p></p>"
        head_html = f"<h2>{_esc(heading)}</h2>" if heading else ""
        item.content = f"{head_html}{body}"
        book.add_item(item)
        spine.append(item)
        if heading:
            toc.append(item)

    # Если заголовков не было — одну запись оглавления на книгу.
    book.toc = tuple(toc) if toc else (spine[0],) if spine else ()
    book.add_item(epub.EpubNcx())
    book.spine = spine

    out = Path(tempfile.mkdtemp(prefix="epub_")) / "book.epub"
    epub.write_epub(str(out), book)
    return out
