"""Документация Python как книги: адрес части, версия-как-метрика, разрез epub.

Сети здесь нет: мастер-архив собирается синтетический, версия не спрашивается.
Проверяется то, что ломается молча — единицы измерения обновления и ссылки,
ведущие за пределы части.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backend.accounts.monitor import _metric_kind
from backend.downloaders import pythondocs as pd
from backend.downloaders.base import UnsupportedURL

BASE = "https://docs.python.org/3/"


def test_part_of_maps_urls():
    assert pd.part_of(BASE + "tutorial/") == "tutorial"
    assert pd.part_of(BASE + "howto/descriptor.html") == "howto"
    assert pd.part_of(BASE + "library/functions.html") == "library"
    assert pd.part_of(BASE + "glossary.html") == "misc"
    # distributing — один документ, живёт в книге про установку модулей
    assert pd.part_of(BASE + "distributing/index.html") == "installing"
    # версия в пути не меняет раздела: документацию всё равно берём актуальную
    assert pd.part_of("https://docs.python.org/3.14/faq/general.html") == "faq"


def test_part_of_rejects_foreign_and_unknown():
    with pytest.raises(UnsupportedURL):
        pd.part_of("https://example.com/3/tutorial/")
    with pytest.raises(UnsupportedURL):
        pd.part_of(BASE + "no-such-section/index.html")


def test_version_int_is_monotonic_across_releases():
    assert pd.version_int(3, 14, 7) == 31407
    assert pd.version_int(3, 14, 7) < pd.version_int(3, 14, 10)
    assert pd.version_int(3, 14, 99) < pd.version_int(3, 15, 0)
    assert pd.version_int(3, 99, 0) < pd.version_int(4, 0, 0)


def test_metric_kind_knows_version_units():
    """Главная ловушка: единица обязана узнаваться и в URL, и в ГОЛОМ хосте.

    `Monitored.last_seen_source` хранит хост без схемы, и распознавание через
    urlparse().hostname вернуло бы там None → «chapters» → сравнение номера
    версии с числом глав → вечная перекачка.
    """
    assert _metric_kind(BASE + "tutorial/") == "version"
    assert _metric_kind("docs.python.org") == "version"
    assert _metric_kind("https://readli.net/chitat-online/?b=1") == "pages"
    assert _metric_kind("https://ficbook.net/readfic/123") == "chapters"


def _master(tmp_path: Path) -> Path:
    """Мини-копия официального архива: два раздела, картинка, перекрёстные ссылки."""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">
  <metadata/>
  <manifest>
    <item id="a" href="tutorial/index.xhtml" media-type="application/xhtml+xml"/>
    <item id="b" href="tutorial/intro.xhtml" media-type="application/xhtml+xml"/>
    <item id="c" href="library/functions.xhtml" media-type="application/xhtml+xml"/>
    <item id="d" href="glossary.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="a"/><itemref idref="b"/><itemref idref="c"/><itemref idref="d"/>
  </spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>
  <navPoint id="n1" playOrder="1"><navLabel><text>The Python Tutorial</text></navLabel>
    <content src="tutorial/index.xhtml"/></navPoint>
  <navPoint id="n2" playOrder="2"><navLabel><text>Standard Library</text></navLabel>
    <content src="library/functions.xhtml"/></navPoint>
  <navPoint id="n3" playOrder="3"><navLabel><text>Glossary</text></navLabel>
    <content src="glossary.xhtml"/></navPoint>
</navMap></ncx>"""
    path = tmp_path / "master.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("content.opf", opf)
        z.writestr("toc.ncx", ncx)
        z.writestr("_static/pygments.css", "body{}")
        z.writestr("_images/pic.png", b"\x89PNG")
        z.writestr(
            "tutorial/index.xhtml",
            '<html><head><title>The Python Tutorial — Python 3.14.7 documentation'
            "</title></head><body>"
            '<a href="intro.xhtml">внутрь части</a>'
            '<a href="../library/functions.xhtml#abs">в другую часть</a>'
            '<a href="https://peps.python.org/">наружу</a>'
            '<img src="../_images/pic.png"/></body></html>',
        )
        z.writestr(
            "tutorial/intro.xhtml",
            "<html><head><title>Whetting Your Appetite — Python 3.14.7 documentation"
            '</title></head><body><a href="../glossary.xhtml">глоссарий</a></body></html>',
        )
        z.writestr("library/functions.xhtml", "<html><body>lib</body></html>")
        z.writestr("glossary.xhtml", "<html><body>glossary</body></html>")
    return path


def test_build_part_keeps_only_its_own_documents(tmp_path):
    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert "tutorial/index.xhtml" in names and "tutorial/intro.xhtml" in names
        assert "library/functions.xhtml" not in names
        assert "glossary.xhtml" not in names
        # общие ресурсы и картинки, на которые часть ссылается, едут с ней
        assert "_static/pygments.css" in names and "_images/pic.png" in names
        # mimetype первым и без сжатия — иначе часть читалок не опознаёт epub
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED


def test_build_part_rewrites_only_outside_links(tmp_path):
    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        html = z.read("tutorial/index.xhtml").decode()
        intro = z.read("tutorial/intro.xhtml").decode()
    # внутри части ссылка остаётся относительной
    assert 'href="intro.xhtml"' in html
    # за пределы части — абсолютной и на .html, иначе она ведёт в никуда
    assert 'href="https://docs.python.org/3/library/functions.html#abs"' in html
    assert 'href="https://docs.python.org/3/glossary.html"' in intro
    # чужие адреса не трогаем
    assert 'href="https://peps.python.org/"' in html


def test_toc_covers_every_document(tmp_path):
    """Раздел, представленный в оригинале ОДНОЙ точкой оглавления (howto), не
    должен превращаться в книгу с одним пунктом — по ней нельзя навигировать."""
    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        ncx = z.read("toc.ncx").decode()
    assert "tutorial/index.xhtml" in ncx
    assert "tutorial/intro.xhtml" in ncx
    # заголовок взят из документа и очищен от хвоста «— Python 3.14.7 …»
    assert "<text>Whetting Your Appetite</text>" in ncx


def test_part_has_embedded_cover(tmp_path):
    """Обложка вшита В КНИГУ, а не проставлена в БД отдельным шагом.

    Так она переживает каждое обновление версии сама: после замены файла
    `_apply_file` достаёт её из epub тем же путём, что и у обычных книг, и
    ленивая ИИ-генерация обложек к этим книгам не подключается.
    """
    from backend.app import covers

    out = pd.build_part(_master(tmp_path), "tutorial", "3.14.7", tmp_path / "part.epub")
    with zipfile.ZipFile(out) as z:
        opf = z.read("content.opf").decode()
        assert "cover.png" in z.namelist()
        # объявлена и по-epub3, и по-epub2: читалки ищут по-разному
        assert 'properties="cover-image"' in opf
        assert '<meta name="cover" content="cover-img"/>' in opf
        # первой страницей книги, иначе обложку никто не увидит при чтении
        assert opf.index('idref="cover-page"') < opf.index('idref="i0"')
    # извлекатель обложек самой читалки обязан её найти
    assert covers.extract_cover(out, "epub", "pytest-pythondocs") is not None


def test_download_result_carries_version_metric(tmp_path, monkeypatch):
    master = _master(tmp_path)
    monkeypatch.setattr(pd, "current_version", lambda: ("3.14.7", 31407))
    monkeypatch.setattr(pd, "fetch_master", lambda ver, vint: master)
    res = pd.download(BASE + "tutorial/")
    assert res.site == "pythondocs"
    assert res.source_url == BASE + "tutorial/"
    # название БЕЗ версии: иначе следующий релиз заведёт вторую книгу-дубль
    assert "3.14" not in res.title
    assert res.extra["update_metric"] == 31407
    assert res.extra["authoritative"] is True
    Path(res.file_path).unlink(missing_ok=True)
