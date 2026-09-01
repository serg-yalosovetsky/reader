"""spec.reader.update-pipeline v8 — файл с БО́ЛЬШИМ числом глав не должен
отвергаться из-за того, что текста в нём меньше.

Живой случай (2026-09-01), work 58 «Сломанный Меч»:
  текущий файл  — 77 глав, richness 4 537 924 (пришёл с более «толстого» зеркала)
  свежий ficbook — 78 глав, richness 4 471 769
`fuller` (по тексту) ложен, `better_structure` (по _real_chapters) ложен, и
78-я глава не могла доехать НИ ПРИ КАКОЙ докачке: карточка вечно показывала
«77 гл. из 78».

Объём текста может УМЕНЬШАТЬСЯ при РОСТЕ числа глав — значит он не может быть
единственным критерием полноты.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from sqlmodel import Session

from backend.app import services
from backend.app.db.models import Work
from backend.downloaders.base import DownloadResult

TITLE = "Сломанный Меч"


def _epub(path: Path, chapters: list[tuple[str, str]]) -> Path:
    """Минимальный EPUB: по секции на главу, TOC из их заголовков."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        items, refs, navs = [], [], []
        for i, (ttl, body) in enumerate(chapters, 1):
            name = f"ch{i}.xhtml"
            z.writestr(name, f"<html><body><h2>{ttl}</h2><p>{body}</p></body></html>")
            items.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="c{i}"/>')
            navs.append(
                f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{ttl}</text>'
                f"</navLabel><content src=\"{name}\"/></navPoint>"
            )
        z.writestr(
            "toc.ncx",
            '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
            'version="2005-1"><navMap>' + "".join(navs) + "</navMap></ncx>",
        )
        z.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="2.0" unique-identifier="i"><metadata/><manifest>'
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            + "".join(items)
            + '</manifest><spine toc="ncx">'
            + "".join(refs)
            + "</spine></package>",
        )
    return path


def _register(session: Session, path: Path, title: str = TITLE) -> Work:
    return services.register_download(
        DownloadResult(
            title=title,
            author="Atlet123",
            site="ficbook",
            source_url="https://ficbook.net/readfic/018abe74",
            file_path=str(path),
            file_format="epub",
            num_chapters=0,
        ),
        session,
    )


def test_more_chapters_wins_even_with_less_text(session: Session, tmp_path):
    """78 глав побеждают 77, хотя текста в них меньше (в пределах 90%).

    Форма случая взята с живого work 58: часть глав на ficbook названа
    «Часть N», и _real_chapters их выбрасывает — структурная метрика показывает
    УМЕНЬШЕНИЕ (76 против 77) при реальном РОСТЕ числа глав (78 против 77).
    Ни fuller, ни better_structure не срабатывают, и новая глава не доезжает
    НИ ПРИ КАКОЙ докачке."""
    old = _epub(
        tmp_path / "old.epub",
        [(f"Глава {i}", "текст " * 1000) for i in range(1, 78)],
    )
    # 78 секций, но две из них — плейсхолдеры для _real_chapters (на ficbook это
    # настоящие авторские названия), поэтому real = 76 < 77.
    new_ch = [(f"Глава {i}", "текст " * 970) for i in range(1, 77)]
    new_ch += [("Часть 1", "текст " * 970), ("Часть 2", "текст " * 970)]
    new = _epub(tmp_path / "new.epub", new_ch)

    # Форма случая: глав БОЛЬШЕ, текста МЕНЬШЕ, структурно — НЕ лучше.
    assert services.count_sections(str(old), "epub", book_title=TITLE) == 77
    assert services.count_sections(str(new), "epub", book_title=TITLE) == 78
    assert services._real_chapters(str(new), "epub") < services._real_chapters(
        str(old), "epub"
    )
    r_old = services._richness(str(old), "epub")
    r_new = services._richness(str(new), "epub")
    assert r_new < r_old and r_new >= r_old * 0.9

    w = _register(session, old)
    assert services.count_sections(w.file_path, "epub", book_title=TITLE) == 77

    w = _register(session, new)
    assert services.count_sections(w.file_path, "epub", book_title=TITLE) == 78, (
        "файл с новой главой обязан заменить старый: объём текста может падать "
        "при росте числа глав, и одним им полноту мерить нельзя"
    )


def test_truncated_file_with_many_tiny_chapters_does_not_win(session: Session, tmp_path):
    """Порог 90% защищает от обрезанного зеркала с кучей мелких секций."""
    old = _epub(
        tmp_path / "old2.epub",
        [(f"Глава {i}", "текст " * 1000) for i in range(1, 78)],
    )
    truncated = _epub(
        tmp_path / "trunc.epub",
        [(f"Часть {i}", "текст " * 10) for i in range(1, 200)],
    )

    w = _register(session, old, title="Другая книга")
    kept = w.file_path
    w = _register(session, truncated, title="Другая книга")
    assert w.file_path == kept, "обрезанный файл не должен вытеснять полный"
