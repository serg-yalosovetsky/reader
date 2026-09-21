"""Название и автор загружаемой книги берутся из файла (serg/tasks#1055).

Живой случай 21.09.2026: fb2 «The Lord of the Rings» загрузили как `39794961.fb2`
и получили книгу «39794961» без автора — загрузка брала имя файла, хотя
`<title-info>` внутри содержал и `<book-title>`, и `<author>`.
"""

from __future__ import annotations

import io
import shutil
import zipfile

import pytest

from backend.app.upload_identity import clean_author, clean_title, extract_identity

LOTR_TITLE = "The Lord of the Rings: The Fellowship of the Ring, The Two Towers, The Return of the King"


def _fb2(*, book_title="", authors=(("John", "Ronald Reuel", "Tolkien"),), body_title="", enc="utf-8"):
    auth = "".join(
        f"<author><first-name>{f}</first-name><middle-name>{m}</middle-name>"
        f"<last-name>{l}</last-name></author>"
        for f, m, l in authors
    )
    title = f"<book-title>{book_title}</book-title>" if book_title else ""
    body = f"<body><title>{body_title}</title><section><p>text</p></section></body>" if body_title else "<body><section><p>text</p></section></body>"
    xml = (
        f'<?xml version="1.0" encoding="{enc}"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description>'
        f"<title-info><genre>sf</genre>{auth}{title}</title-info>"
        "<document-info><author><first-name>Some</first-name><last-name>Scanner</last-name></author></document-info>"
        f"</description>{body}</FictionBook>"
    )
    return xml.encode(enc)


def _epub(*, title="", creators=(), first_page=""):
    cre = "".join(
        f'<dc:creator{(" opf:role=" + chr(34) + role + chr(34)) if role else ""}>{name}</dc:creator>'
        for name, role in creators
    )
    ttl = f"<dc:title>{title}</dc:title>" if title else ""
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf" version="2.0">'
        f"<metadata>{ttl}{cre}</metadata>"
        '<manifest><item id="p1" href="page1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="p1"/></spine></package>'
    )
    container = (
        '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/page1.xhtml", f"<html><body>{first_page}</body></html>")
    return buf.getvalue()


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --- метаданные fb2 -----------------------------------------------------------


def test_fb2_metadata_beats_the_filename_number(tmp_path):
    """Тот самый случай: имя файла — номер, а название и автор лежат в title-info."""
    p = _write(tmp_path, "39794961.fb2", _fb2(book_title=LOTR_TITLE))
    assert extract_identity(p, "fb2", "39794961") == (LOTR_TITLE, "John Ronald Reuel Tolkien")


def test_fb2_document_info_author_is_not_taken(tmp_path):
    """Автор — из title-info. В document-info записан тот, кто сделал fb2 (сканер)."""
    p = _write(tmp_path, "b.fb2", _fb2(book_title="Dune"))
    assert extract_identity(p, "fb2", "b")[1] == "John Ronald Reuel Tolkien"
    assert "Scanner" not in extract_identity(p, "fb2", "b")[1]


def test_fb2_several_authors_and_nickname(tmp_path):
    data = _fb2(book_title="Duet", authors=(("Ann", "", "One"), ("", "", "")))
    data = data.replace(
        b"<author><first-name></first-name><middle-name></middle-name><last-name></last-name></author>",
        b"<author><nickname>NickTwo</nickname></author>",
    )
    p = _write(tmp_path, "d.fb2", data)
    assert extract_identity(p, "fb2", "d") == ("Duet", "Ann One, NickTwo")


def test_fb2_windows_1251_is_decoded(tmp_path):
    p = _write(
        tmp_path, "x.fb2",
        _fb2(book_title="Мастер и Маргарита", authors=(("Михаил", "Афанасьевич", "Булгаков"),), enc="windows-1251"),
    )
    assert extract_identity(p, "fb2", "x") == ("Мастер и Маргарита", "Михаил Афанасьевич Булгаков")


# --- титульный экран ----------------------------------------------------------


def test_titlepage_is_used_when_metadata_title_is_a_placeholder(tmp_path):
    """book-title «39794961» — заглушка: берём титульный экран и снимаем «BY»."""
    p = _write(
        tmp_path, "39794961.fb2",
        _fb2(book_title="39794961", authors=(), body_title="<p>THE LORD OF THE RINGS</p><p><strong>BY J.R.R. TOLKIEN</strong></p>"),
    )
    assert extract_identity(p, "fb2", "39794961") == ("THE LORD OF THE RINGS", "J.R.R. TOLKIEN")


def test_metadata_wins_over_titlepage(tmp_path):
    p = _write(
        tmp_path, "a.fb2",
        _fb2(book_title=LOTR_TITLE, body_title="<p>THE LORD OF THE RINGS</p><p>BY J.R.R. TOLKIEN</p>"),
    )
    assert extract_identity(p, "fb2", "a") == (LOTR_TITLE, "John Ronald Reuel Tolkien")


def test_titlepage_without_by_line_gives_title_only_for_fb2(tmp_path):
    p = _write(tmp_path, "a.fb2", _fb2(authors=(), body_title="<p>Просто Заголовок</p>"))
    assert extract_identity(p, "fb2", "a") == ("Просто Заголовок", "")


# --- epub ---------------------------------------------------------------------


def test_epub_title_and_creator(tmp_path):
    p = _write(tmp_path, "e.epub", _epub(title="Dune", creators=[("Frank Herbert", "aut")]))
    assert extract_identity(p, "epub", "e") == ("Dune", "Frank Herbert")


def test_epub_translator_is_not_the_author(tmp_path):
    p = _write(
        tmp_path, "e.epub",
        _epub(title="Война и мир", creators=[("Лев Толстой", "aut"), ("Louise Maude", "trl")]),
    )
    assert extract_identity(p, "epub", "e") == ("Война и мир", "Лев Толстой")


def test_epub_first_page_needs_a_by_line(tmp_path):
    """Первая страница epub — часто копирайт. Без строки «BY» из неё ничего не берём."""
    only_copyright = _write(tmp_path, "c.epub", _epub(first_page="<p>Copyright 2020</p><p>All rights reserved</p>"))
    assert extract_identity(only_copyright, "epub", "c") == ("", "")
    titled = _write(tmp_path, "t.epub", _epub(first_page="<h1>The Hobbit</h1><p>By J.R.R. Tolkien</p>"))
    assert extract_identity(titled, "epub", "t") == ("The Hobbit", "J.R.R. Tolkien")


# --- pdf ----------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("pdfinfo"), reason="нет poppler")
def test_pdf_info_dictionary(tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(buf, "PDF", title="Refactoring", author="Martin Fowler")
    p = _write(tmp_path, "r.pdf", buf.getvalue())
    assert extract_identity(p, "pdf", "r") == ("Refactoring", "Martin Fowler")


# --- заглушки и чистка --------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["", "   ", "39794961", "123456", "---", "...", "Unknown", "untitled", "Без названия", "Microsoft Word - report.docx", "report.docx"],
)
def test_placeholder_titles_are_rejected(title):
    assert clean_title(title) == ""


@pytest.mark.parametrize("title", ["1984", "451", "2001", "12345", "11/22/63", "Fahrenheit 451"])
def test_real_titles_made_of_digits_are_kept(title):
    """«1984» — настоящее название (в библиотеке есть книга 74). Правило «нет букв — заглушка»
    отбрасывало бы её; номера каталогов длиннее — 6–9 цифр."""
    assert clean_title(title) == title


def test_numeric_real_title_survives_a_different_filename(tmp_path):
    p = _write(tmp_path, "orwell.fb2", _fb2(book_title="1984", authors=(("George", "", "Orwell"),)))
    assert extract_identity(p, "fb2", "orwell") == ("1984", "George Orwell")


def test_title_equal_to_filename_is_a_placeholder():
    assert clean_title("my-book_v2", "my book v2") == ""
    assert clean_title("Dune", "dune-1965") == "Dune"


@pytest.mark.parametrize("author", ["", "Unknown", "Неизвестный автор", "Anonymous", "-", "123"])
def test_placeholder_authors_are_rejected(author):
    assert clean_author(author) == ""


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("BY J.R.R. TOLKIEN", "J.R.R. TOLKIEN"),
        ("By Frank Herbert", "Frank Herbert"),
        ("  by   Frank Herbert ", "Frank Herbert"),
        ("Автор: Лев Толстой", "Лев Толстой"),
        ("Byron", "Byron"),  # «By» без пробела — начало настоящей фамилии
        ("Author Name", "Author Name"),  # «Author» без двоеточия — не префикс
    ],
)
def test_by_prefix_is_stripped_only_as_a_prefix(raw, clean):
    assert clean_author(raw) == clean


# --- сбой разбора не роняет загрузку ------------------------------------------


@pytest.mark.parametrize("fmt", ["fb2", "epub", "pdf"])
def test_garbage_file_gives_empty_identity_not_an_exception(tmp_path, fmt):
    p = _write(tmp_path, f"g.{fmt}", b"\x00\x01 not a book at all")
    assert extract_identity(p, fmt, "g") == ("", "")


def test_missing_file_gives_empty_identity(tmp_path):
    assert extract_identity(tmp_path / "nope.fb2", "fb2", "nope") == ("", "")


# --- через HTTP: одиночный файл и zip -----------------------------------------


def test_upload_fills_title_and_author(client):
    data = _fb2(book_title=LOTR_TITLE)
    r = client.post("/api/library/upload", files={"file": ("39794961.fb2", data, "application/x-fictionbook+xml")})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["title"] == LOTR_TITLE
    assert w["author"] == "John Ronald Reuel Tolkien"


def test_upload_of_unreadable_book_keeps_the_filename_title(client):
    """Регресс: то, что раньше работало, работает по-прежнему."""
    r = client.post("/api/library/upload", files={"file": ("Моя книга.epub", b"not-a-zip", "application/epub+zip")})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Моя книга"
    assert r.json()["author"] == ""


def test_zip_upload_fills_title_and_author_for_every_book(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("39794961.fb2", _fb2(book_title="Alpha", authors=(("A", "", "One"),)))
        z.writestr("sub/1234.fb2", _fb2(book_title="Beta", authors=(("B", "", "Two"),)))
    r = client.post("/api/library/upload", files={"file": ("pack.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    got = {w["title"] for w in r.json()["works"]}
    assert got == {"Alpha", "Beta"}
    lib = {w["title"]: w["author"] for w in client.get("/api/library").json()}
    assert lib["Alpha"] == "A One" and lib["Beta"] == "B Two"
