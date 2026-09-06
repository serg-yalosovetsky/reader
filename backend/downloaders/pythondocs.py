"""Официальная документация Python как книги читалки.

Документация публикуется ОДНИМ epub (`archives/python-<M.m>-docs.epub`, ~9 МБ,
567 документов). Читать её так нельзя: «Учебник» и «Стандартная библиотека» —
разные книги с разным прогрессом, закладками и переводом. Поэтому адаптер режет
официальный файл на ЧАСТИ по верхнему уровню оглавления, по одной книге на
часть, и собирает из каждой самостоятельный epub.

Второе отличие от фанфиков: у документации не бывает «новых глав». Она
обновляется ВЕРСИЯМИ (3.14.6 → 3.14.7 → 3.15.0), и признак обновления —
НОМЕР ВЕРСИИ, закодированный целым (`major*10000 + minor*100 + micro`).
Монитор сравнивает это число как «главы», поэтому единица объявлена явно
(`monitor._metric_kind` → "version"); подробности и грабли — в
spec.reader.python-docs.
"""

from __future__ import annotations

import logging
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
# defusedxml защищает от XXE/billion-laughs: XML приезжает из сети, пусть и
# с python.org. Тот же выбор, что в calibre/client.py.
from defusedxml import ElementTree as ET

from ..app.config import TMP_DIR
from . import pythondocs_cover
from .base import DownloaderError, DownloadResult, UnsupportedURL

log = logging.getLogger("reader.pythondocs")

HOST = "docs.python.org"
BASE = "https://docs.python.org/3/"
AUTHOR = "Python Software Foundation"
TIMEOUT = 120.0

# Часть документации = книга. `dirs` — каталоги внутри официального epub,
# `roots` — отдельные файлы в его корне. Порядок словаря = порядок оглавления
# оригинала, по нему же удобно заводить книги пачкой.
PARTS: dict[str, dict] = {
    "tutorial": {
        "title": "Python — Учебник",
        "path": "tutorial/",
        "dirs": ("tutorial",),
        "roots": (),
    },
    "library": {
        "title": "Python — Стандартная библиотека",
        "path": "library/",
        "dirs": ("library",),
        "roots": (),
    },
    "reference": {
        "title": "Python — Справочник по языку",
        "path": "reference/",
        "dirs": ("reference",),
        "roots": (),
    },
    "howto": {
        "title": "Python — HOWTO, практические руководства",
        "path": "howto/",
        "dirs": ("howto",),
        "roots": (),
    },
    "using": {
        "title": "Python — Установка и запуск",
        "path": "using/",
        "dirs": ("using",),
        "roots": (),
    },
    "installing": {
        "title": "Python — Установка и публикация модулей",
        "path": "installing/",
        "dirs": ("installing", "distributing"),
        "roots": (),
    },
    "extending": {
        "title": "Python — Расширение и встраивание",
        "path": "extending/",
        "dirs": ("extending",),
        "roots": (),
    },
    "c-api": {
        "title": "Python — Справочник Python/C API",
        "path": "c-api/",
        "dirs": ("c-api",),
        "roots": (),
    },
    "faq": {
        "title": "Python — Частые вопросы (FAQ)",
        "path": "faq/",
        "dirs": ("faq",),
        "roots": (),
    },
    "whatsnew": {
        "title": "Python — Что нового в каждой версии",
        "path": "whatsnew/",
        "dirs": ("whatsnew",),
        "roots": (),
    },
    "deprecations": {
        "title": "Python — Устаревшее и удаляемое",
        "path": "deprecations/",
        "dirs": ("deprecations",),
        "roots": (),
    },
    # Корневые страницы оригинала: глоссарий и служебные разделы. Отдельной
    # книгой, потому что глоссарий читают, а не листают из оглавления.
    "misc": {
        "title": "Python — Глоссарий и о документации",
        "path": "glossary.html",
        "dirs": (),
        "roots": (
            "glossary.xhtml",
            "about.xhtml",
            "bugs.xhtml",
            "copyright.xhtml",
            "license.xhtml",
        ),
    },
}

# Логотип Python внутри официального архива (он же og:image документации).
LOGO_ASSET = "_static/og-image.png"

MEDIA = {
    ".xhtml": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
OPF_NS = "http://www.idpf.org/2007/opf"

_VER_JS = re.compile(r"VERSION:\s*'(\d+)\.(\d+)\.(\d+)'")
_VER_HTML = re.compile(r"Python (\d+)\.(\d+)\.(\d+)")
_IMG_REF = re.compile(r"(?:\.\./)*_images/([A-Za-z0-9_.\-]+)")


# --------------------------------------------------------------------------
# адрес части
# --------------------------------------------------------------------------
def supports(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith(HOST)


def part_url(key: str) -> str:
    return BASE + PARTS[key]["path"]


def part_of(url: str) -> str:
    """Ключ части по адресу. `https://docs.python.org/3/tutorial/` → tutorial.

    Корневые страницы (glossary/about/bugs/copyright/license) — часть `misc`.
    """
    if not supports(url):
        raise UnsupportedURL(f"не docs.python.org: {url}")
    path = (urlparse(url).path or "").lstrip("/")
    # /3/… и /3.14/… — одна и та же документация, версия берётся с сайта.
    path = re.sub(r"^(3(\.\d+)?|dev|latest)/", "", path)
    head = path.split("/", 1)[0]
    if head in PARTS:
        return head
    if head in ("distributing",):
        return "installing"
    stem = head.split(".", 1)[0]
    if f"{stem}.xhtml" in PARTS["misc"]["roots"] or head == "":
        return "misc"
    raise UnsupportedURL(
        f"раздел документации не поддерживается: {url}. "
        f"Известные: {', '.join(part_url(k) for k in PARTS)}"
    )


# --------------------------------------------------------------------------
# версия и мастер-файл
# --------------------------------------------------------------------------
def current_version() -> tuple[str, int]:
    """Версия документации на сайте: ('3.14.7', 31407).

    Спрашиваем `_static/documentation_options.js` — это несколько сотен байт,
    а не главная страница на сотни килобайт: функция вызывается монитором на
    КАЖДОМ тике по каждой из книг.
    """
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as c:
            r = c.get(BASE + "_static/documentation_options.js")
            m = _VER_JS.search(r.text) if r.status_code < 400 else None
            if not m:
                r = c.get(BASE)
                m = _VER_HTML.search(r.text)
    except httpx.HTTPError as e:
        raise DownloaderError(f"docs.python.org недоступен: {e}") from e
    if not m:
        raise DownloaderError("не удалось определить версию документации Python")
    major, minor, micro = (int(x) for x in m.groups())
    return f"{major}.{minor}.{micro}", version_int(major, minor, micro)


def version_int(major: int, minor: int, micro: int) -> int:
    """Версия одним монотонным числом: 3.14.7 → 31407, 3.15.0 → 31500.

    Монитор умеет сравнивать только «больше/меньше», поэтому кодировка обязана
    расти вместе с релизом. micro ограничен 99 — за всю историю CPython столько
    патч-релизов у одной ветки не выходило.
    """
    return major * 10000 + minor * 100 + micro


def count_chapters(url: str) -> int | None:
    """Метрика обновления для монитора — НОМЕР ВЕРСИИ, а не число глав.

    Единица объявлена в `monitor._metric_kind` как "version": сравнивать это
    число с количеством секций в файле нельзя (см. spec.reader.python-docs).
    """
    part_of(url)  # неизвестный раздел — не наше дело
    return current_version()[1]


def _master_path(ver: str, vint: int) -> Path:
    minor = ".".join(ver.split(".")[:2])
    return TMP_DIR / "pythondocs" / f"python-{minor}-docs-{vint}.epub"


def fetch_master(ver: str, vint: int) -> Path:
    """Официальный epub целиком, с кэшем по версии.

    12 книг = 12 подписок, и каждая на своём тике попросила бы 9 МБ. Скачиваем
    во временный файл рядом и переименовываем: `check_all` обходит подписки в
    потоках, и половинчатый файл не должен стать «кэшем».
    """
    dest = _master_path(ver, vint)
    if dest.exists() and dest.stat().st_size > 1_000_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    minor = ".".join(ver.split(".")[:2])
    url = f"{BASE}archives/python-{minor}-docs.epub"
    fd, tmp_name = tempfile.mkstemp(suffix=".epub", dir=str(dest.parent))
    tmp = Path(tmp_name)
    try:
        import os

        os.close(fd)
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise DownloaderError(
                        f"архив документации не отдан ({r.status_code}): {url}"
                    )
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
        if not zipfile.is_zipfile(tmp):
            raise DownloaderError(f"архив документации не похож на epub: {url}")
        tmp.replace(dest)
    except httpx.HTTPError as e:
        raise DownloaderError(f"не удалось скачать {url}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    # Старые версии в кэше не нужны: место дороже повторной загрузки раз в месяц.
    for old in dest.parent.glob("python-*-docs-*.epub"):
        if old != dest:
            old.unlink(missing_ok=True)
    return dest


# --------------------------------------------------------------------------
# сборка части
# --------------------------------------------------------------------------
def _spine_hrefs(opf_xml: str) -> list[str]:
    """Документы мастера в порядке чтения (spine), href'ы как в манифесте."""
    root = ET.fromstring(opf_xml)
    manifest = {}
    for item in root.iter(f"{{{OPF_NS}}}item"):
        manifest[item.get("id")] = item.get("href")
    out = []
    for ref in root.iter(f"{{{OPF_NS}}}itemref"):
        href = manifest.get(ref.get("idref"))
        if href:
            out.append(href)
    return out


def _in_part(href: str, part: dict) -> bool:
    if href in part["roots"]:
        return True
    return any(href.startswith(d + "/") for d in part["dirs"])


def _nav_tree(ncx_xml: str, part: dict) -> list[dict]:
    """Поддерево оглавления мастера, относящееся к части.

    Берём ТОЛЬКО верхнеуровневые точки части: у документации это ровно один
    раздел (или несколько корневых страниц у `misc`), а вложенность внутри
    сохраняется как есть.
    """
    root = ET.fromstring(ncx_xml)

    def walk(node) -> list[dict]:
        out = []
        for np in node.findall(f"{{{NCX_NS}}}navPoint"):
            label = np.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
            content = np.find(f"{{{NCX_NS}}}content")
            src = (content.get("src") or "") if content is not None else ""
            out.append(
                {
                    "title": (label.text or "").strip() if label is not None else "",
                    "src": src,
                    "children": walk(np),
                }
            )
        return out

    top = walk(root.find(f"{{{NCX_NS}}}navMap"))
    return [n for n in top if _in_part(n["src"].split("#", 1)[0], part)]


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_TITLE_TAIL = re.compile(r"\s*[—–-]\s*Python\s+[\d.]+.*$", re.S)


def _doc_title(html: str, fallback: str) -> str:
    """Заголовок документа из <title>, без хвоста «— Python 3.14.7 documentation»."""
    m = _TITLE_RE.search(html)
    if not m:
        return fallback
    t = re.sub(r"\s+", " ", m.group(1)).strip()
    t = _TITLE_TAIL.sub("", t).strip()
    return t or fallback


def _complete_tree(tree: list[dict], docs: list[str], bodies: dict[str, str]) -> list[dict]:
    """Дополнить оглавление документами, которых в нём нет.

    В оглавлении оригинала целые разделы представлены ОДНОЙ точкой: у «Python
    HOWTOs» вложенных пунктов нет вовсе, хотя документов 29. Книга с одним
    пунктом оглавления нечитаема — навигация по ней невозможна, и `count_sections`
    честно показывает «1 глава». Недостающие документы добавляются в порядке
    spine, заголовок берётся из самого документа.
    """
    covered: set[str] = set()

    def collect(nodes: list[dict]) -> None:
        for n in nodes:
            covered.add(n["src"].split("#", 1)[0])
            collect(n["children"])

    collect(tree)
    top = {n["src"].split("#", 1)[0]: n for n in tree}
    out: list[dict] = []
    for href in docs:
        if href in top:
            out.append(top[href])
        elif href not in covered:
            out.append(
                {
                    "title": _doc_title(bodies.get(href, ""), href),
                    "src": href,
                    "children": [],
                }
            )
    return out or tree


def _rewrite_links(html: str, self_path: str, keep: set[str], known: set[str]) -> str:
    """Ссылки ЗА пределы части — на сайт, внутри части — как есть.

    После разреза половина перекрёстных ссылок документации ведёт в файлы,
    которых в этой книге нет. Оставить их — значит отдать читателю мёртвую
    ссылку; переписываем в абсолютный `https://docs.python.org/3/…`.
    """
    base_dir = posixpath.dirname(self_path)

    def sub(m: re.Match) -> str:
        attr, value = m.group(1), m.group(2)
        if not value or value[0] in "#?" or "://" in value or value.startswith("mailto:"):
            return m.group(0)
        path, _, anchor = value.partition("#")
        target = posixpath.normpath(posixpath.join(base_dir, path)) if path else self_path
        if target in keep:
            return m.group(0)
        if target in known:
            site = target[:-6] + ".html" if target.endswith(".xhtml") else target
            url = BASE + site + (("#" + anchor) if anchor else "")
            return f'{attr}="{url}"'
        return m.group(0)

    return re.sub(r'\b(href)="([^"]*)"', sub, html)


def _opf(key: str, ver: str, files: list[str], spine: list[str], cover: bool) -> str:
    part = PARTS[key]
    items = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>',
    ]
    if cover:
        # Обложку объявляем ОБОИМИ способами: `properties="cover-image"` (epub3) и
        # `<meta name="cover">` (epub2). Читалки и каталогизаторы ищут по-разному,
        # а извлекатель обложек читалки (covers._epub_cover) начинает со второго.
        items += [
            '<item id="cover-img" href="cover.png" media-type="image/png" '
            'properties="cover-image"/>',
            '<item id="cover-page" href="cover.xhtml" '
            'media-type="application/xhtml+xml"/>',
        ]
    ids = {}
    for i, href in enumerate(files):
        ext = posixpath.splitext(href)[1].lower()
        ids[href] = f"i{i}"
        items.append(
            f'<item id="i{i}" href="{href}" '
            f'media-type="{MEDIA.get(ext, "application/octet-stream")}"/>'
        )
    refs_list = ['<itemref idref="cover-page"/>'] if cover else []
    refs_list += [f'<itemref idref="{ids[h]}"/>' for h in spine if h in ids]
    refs = "\n    ".join(refs_list)
    desc = (
        f"Официальная документация Python {ver}, раздел «{part['title']}». "
        f"Источник: {part_url(key)}"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="bookid" xml:lang="en">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">pythondocs-{key}</dc:identifier>\n'
        f"    <dc:title>{_esc(part['title'])}</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        f"    <dc:creator>{AUTHOR}</dc:creator>\n"
        f"    <dc:description>{_esc(desc)}</dc:description>\n"
        f"    <dc:source>{part_url(key)}</dc:source>\n"
        f'    <meta property="dcterms:modified">{ver}</meta>\n'
        + ('    <meta name="cover" content="cover-img"/>\n' if cover else "")
        + "  </metadata>\n"
        "  <manifest>\n    " + "\n    ".join(items) + "\n  </manifest>\n"
        f'  <spine toc="ncx">\n    {refs}\n  </spine>\n'
        "</package>\n"
    )


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _ncx(key: str, tree: list[dict]) -> str:
    counter = [0]

    def render(nodes: list[dict], depth: int) -> str:
        out = []
        for n in nodes:
            counter[0] += 1
            i = counter[0]
            pad = "  " * depth
            kids = render(n["children"], depth + 1)
            out.append(
                f'{pad}<navPoint id="n{i}" playOrder="{i}">'
                f"<navLabel><text>{_esc(n['title'])}</text></navLabel>"
                f'<content src="{n["src"]}"/>{kids}</navPoint>'
            )
        return "\n".join(out)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        f'  <head><meta name="dtb:uid" content="pythondocs-{key}"/></head>\n'
        f"  <docTitle><text>{_esc(PARTS[key]['title'])}</text></docTitle>\n"
        "  <navMap>\n" + render(tree, 2) + "\n  </navMap>\n</ncx>\n"
    )


def _nav(key: str, tree: list[dict]) -> str:
    def render(nodes: list[dict]) -> str:
        if not nodes:
            return ""
        li = "".join(
            f'<li><a href="{n["src"]}">{_esc(n["title"])}</a>{render(n["children"])}</li>'
            for n in nodes
        )
        return f"<ol>{li}</ol>"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops"><head>'
        f"<title>{_esc(PARTS[key]['title'])}</title></head><body>"
        f'<nav epub:type="toc" id="toc"><h1>{_esc(PARTS[key]["title"])}</h1>'
        f"{render(tree)}</nav></body></html>\n"
    )


def _cover_page(title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f"<title>{_esc(title)}</title><style>"
        "html,body{margin:0;padding:0;height:100%;text-align:center;background:#0e1620}"
        "img{max-width:100%;max-height:100%}</style></head>"
        f'<body><img src="cover.png" alt="{_esc(title)}"/></body></html>\n'
    )


def build_part(master: Path, key: str, ver: str, out_path: Path | None = None) -> Path:
    """Собрать самостоятельный epub одной части из официального архива."""
    if key not in PARTS:
        raise UnsupportedURL(f"неизвестный раздел документации: {key}")
    part = PARTS[key]
    with zipfile.ZipFile(master) as z:
        names = set(z.namelist())
        opf_xml = z.read("content.opf").decode("utf-8")
        spine = [h for h in _spine_hrefs(opf_xml) if h in names]
        docs = [h for h in spine if _in_part(h, part)]
        if not docs:
            raise DownloaderError(f"в архиве нет раздела «{key}»")
        keep = set(docs)
        tree = _nav_tree(z.read("toc.ncx").decode("utf-8"), part)

        bodies: dict[str, str] = {}
        images: set[str] = set()
        for href in docs:
            html = z.read(href).decode("utf-8")
            for m in _IMG_REF.finditer(html):
                cand = f"_images/{m.group(1)}"
                if cand in names:
                    images.add(cand)
            bodies[href] = _rewrite_links(html, href, keep, names)

        tree = _complete_tree(tree, docs, bodies)
        # Логотип берём из самого архива документации — официальный, и он уже
        # скачан; отдельного запроса за картинкой не нужно.
        logo = z.read(LOGO_ASSET) if LOGO_ASSET in names else None
        cover_png = pythondocs_cover.render(part["title"], ver, key, logo)
        assets = sorted(n for n in names if n.startswith("_static/")) + sorted(images)
        files = docs + assets

        out = Path(out_path) if out_path else Path(
            tempfile.mkstemp(suffix=".epub", prefix=f"pydocs-{key}-")[1]
        )
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            # mimetype обязан лежать первым и БЕЗ сжатия — иначе часть читалок
            # не опознаёт архив как epub.
            w.writestr(
                zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED
            )
            w.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<container version="1.0" '
                'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            w.writestr("content.opf", _opf(key, ver, files, docs, bool(cover_png)))
            if cover_png:
                w.writestr("cover.png", cover_png)
                w.writestr("cover.xhtml", _cover_page(part["title"]))
            w.writestr("toc.ncx", _ncx(key, tree))
            w.writestr("nav.xhtml", _nav(key, tree))
            for href, html in bodies.items():
                w.writestr(href, html)
            for asset in assets:
                w.writestr(asset, z.read(asset))
    return out


# --------------------------------------------------------------------------
# интерфейс загрузчика
# --------------------------------------------------------------------------
def download(url: str, creds: tuple[str, str] | None = None) -> DownloadResult:
    key = part_of(url)
    ver, vint = current_version()
    master = fetch_master(ver, vint)
    path = build_part(master, key, ver)
    part = PARTS[key]
    return DownloadResult(
        file_path=path,
        file_format="epub",
        title=part["title"],
        author=AUTHOR,
        site="pythondocs",
        source_url=part_url(key),
        # Число «глав» книги считает register_download по самому файлу; сюда
        # кладём ВЕРСИЮ отдельным полем, чтобы подписка завелась в правильных
        # единицах (см. extra["update_metric"]).
        num_chapters=0,
        extra={
            "annotation": (
                f"Официальная документация Python, раздел «{part['title']}». "
                f"Версия {ver}. Источник: {part_url(key)}"
            ),
            "docs_version": ver,
            # Метрика подписки: монитор сравнивает именно это число.
            "update_metric": vint,
            # Источник версионирован: новый файл актуальнее по определению,
            # без сравнения объёмов текста (spec.reader.python-docs).
            "authoritative": True,
            "status": "обновляется",
        },
    )
