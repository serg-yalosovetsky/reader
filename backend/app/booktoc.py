"""Оглавление книги на СЕРВЕРЕ — для страницы книги (serg/tasks#1077).

Зачем сервер, если оглавление уже строит foliate-js: на странице книги файла
книги нет. Тянуть ради списка глав весь EPUB (у фанфика это мегабайты, у
«Ring-Maker» — 181 глава) только чтобы показать заголовки — дорого, особенно
с телефона. Здесь тот же список собирается из метаданных файла: для EPUB это
nav/NCX + spine, для FB2 — прямые потомки первого <body>.

Файл книги — НЕДОВЕРЕННЫЙ ввод: книги приезжают автозагрузкой со сторонних
фанфик-сайтов и вручную. Поэтому разбор идёт через defusedxml, а не через
stdlib: он запрещает DTD-сущности, на которых строится «billion laughs», и
внешние ссылки (XXE). Текущий libexpat (2.6+) такую бомбу отбивает и сам
лимитом амплификации — но это свойство системной библиотеки в образе, а не
нашего кода, и меняется оно без нашего ведома.

Соответствие клиенту — не случайность, а требование: клик по главе открывает
читалку и переходит по тем же координатам, которыми оперирует foliate-js.
- `href` для EPUB — путь внутри архива, разрешённый относительно nav/NCX, ровно
  как это делает vendor/foliate-js/epub.js (resolveURL(href, navPath)); его и
  принимает view.goTo().
- `index` — позиция секции: для EPUB это номер в spine (foliate строит sections
  из spine), для FB2 — номер прямого потомка первого <body> (fb2.js делает
  отдельную секцию на каждого такого потомка).
Фронт использует href, а index — фолбэк и подсказка для поиска по тексту.
"""

from __future__ import annotations

import logging
import posixpath
import re
import threading
import zipfile
from pathlib import Path
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring, parse

# Только ради типа Element в аннотациях: разбора этим модулем здесь нет,
# он весь идёт через defusedxml выше.
from xml.etree.ElementTree import Element  # nosec B405

log = logging.getLogger("reader.toc")

NS_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
NS_OPF = "{http://www.idpf.org/2007/opf}"
NS_XHTML = "{http://www.w3.org/1999/xhtml}"
NS_NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
NS_EPUB = "{http://www.idpf.org/2007/ops}"
NS_FB2 = "{http://www.gribuser.ru/xml/fictionbook/2.0}"

# Потолок на число пунктов: защищает и память, и страницу книги. Книг длиннее
# этого в библиотеке нет, но документация/сборники бывают любыми.
MAX_ITEMS = 3000
# Сколько файлов секций разрешено открыть ради заголовков, когда ни nav, ни NCX
# в книге нет. Без потолка «оглавление» распаковывало бы книгу целиком.
MAX_SPINE_PROBE = 400


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _text_of(el: Element | None) -> str:
    if el is None:
        return ""
    return _norm("".join(el.itertext()))


def _resolve(base: str, href: str) -> str:
    """href относительно документа base — как resolveURL в foliate-js."""
    if not href:
        return ""
    href = href.split("#", 1)[0]
    joined = posixpath.join(posixpath.dirname(base), href)
    return posixpath.normpath(joined).lstrip("/")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------- EPUB


def _parse_nav(doc: Element, nav_path: str) -> list[dict]:
    """EPUB3 nav.xhtml → плоский список {label, href, level}."""
    nav_el = None
    for el in doc.iter():
        if _local(el.tag) != "nav":
            continue
        kind = el.get(f"{NS_EPUB}type") or el.get("type") or ""
        if "toc" in kind.split():
            nav_el = el
            break
    if nav_el is None:
        return []

    items: list[dict] = []

    def walk(ol: Element, level: int) -> None:
        for li in ol:
            if _local(li.tag) != "li" or len(items) >= MAX_ITEMS:
                continue
            link = None
            sub = None
            for child in li:
                name = _local(child.tag)
                if name in ("a", "span") and link is None:
                    link = child
                elif name in ("ol", "ul") and sub is None:
                    sub = child
            if link is not None:
                raw = link.get("href") or ""
                label = _text_of(link) or _norm(link.get("title"))
                items.append(
                    {
                        "label": label,
                        "href": _full_href(nav_path, raw),
                        "level": level,
                    }
                )
            if sub is not None:
                walk(sub, level + 1)

    for child in nav_el:
        if _local(child.tag) in ("ol", "ul"):
            walk(child, 0)
    return items


def _full_href(base: str, raw: str) -> str:
    """Путь внутри архива + сохранённый фрагмент (#id) — как ждёт foliate."""
    if not raw:
        return ""
    path, _, frag = raw.partition("#")
    resolved = _resolve(base, path) if path else ""
    if not resolved:
        return ""
    return f"{resolved}#{frag}" if frag else resolved


def _parse_ncx(doc: Element, ncx_path: str) -> list[dict]:
    """EPUB2 toc.ncx → плоский список."""
    nav_map = None
    for el in doc.iter():
        if _local(el.tag) == "navMap":
            nav_map = el
            break
    if nav_map is None:
        return []

    items: list[dict] = []

    def _walk_point(
        point: Element, level: int, out: list[dict], base: str
    ) -> None:
        if len(out) >= MAX_ITEMS:
            return
        label = ""
        href = ""
        children: list[Element] = []
        for child in point:
            name = _local(child.tag)
            if name == "navLabel":
                label = _text_of(child)
            elif name == "content":
                href = _full_href(base, child.get("src") or "")
            elif name == "navPoint":
                children.append(child)
        out.append({"label": label, "href": href, "level": level})
        for child in children:
            _walk_point(child, level + 1, out, base)

    for point in nav_map:
        if _local(point.tag) == "navPoint":
            _walk_point(point, 0, items, ncx_path)
    return items


# FanFicFare пишет в <head> каждой главы ссылку на неё у источника. По этой
# ссылке даты глав (таблица ChapterMeta) сопоставляются с пунктами оглавления:
# номер для этого не годится — в оглавлении бывают служебные страницы вроде
# «Title Page», из-за которых нумерация съезжает.
_CHAPTER_URL_RE = re.compile(
    rb'<meta\s+name="chapterurl"\s+content="([^"]*)"', re.I
)
# Ссылка лежит в <head>, читать файл целиком незачем.
HEAD_BYTES = 4096


def _chapter_url(zf: zipfile.ZipFile, path: str) -> str:
    try:
        with zf.open(path) as fh:
            head = fh.read(HEAD_BYTES)
    except (KeyError, OSError) as e:
        log.warning("не прочитал шапку секции %s: %s", path, e)
        return ""
    m = _CHAPTER_URL_RE.search(head)
    return m.group(1).decode("utf-8", "replace") if m else ""


def _spine_title(zf: zipfile.ZipFile, path: str) -> str:
    """Заголовок секции из самого XHTML: <title>, иначе первый h1–h6."""
    try:
        raw = zf.read(path)
    except KeyError:
        return ""
    try:
        doc = fromstring(raw)
    except (ParseError, DefusedXmlException):
        # XHTML у фанфиков бывает не строгим XML — тогда дешёвый regex.
        text = raw.decode("utf-8", "replace")
        m = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", text, re.S | re.I)
        if not m:
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        return _norm(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
    for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        el = doc.find(f".//{NS_XHTML}{tag}")
        if el is None:
            el = doc.find(f".//{tag}")
        if el is not None:
            text = _text_of(el)
            if text:
                return text
    head_title = doc.find(f".//{NS_XHTML}title") or doc.find(".//title")
    return _text_of(head_title)


def _epub_toc(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        container = fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(f".//{NS_CONTAINER}rootfile")
        opf_path = (rootfile.get("full-path") if rootfile is not None else "") or ""
        if not opf_path:
            return []
        opf = fromstring(zf.read(opf_path))

        manifest: dict[str, tuple[str, str, str]] = {}
        for item in opf.iter(f"{NS_OPF}item"):
            manifest[item.get("id") or ""] = (
                item.get("href") or "",
                item.get("media-type") or "",
                item.get("properties") or "",
            )

        spine_el = opf.find(f"{NS_OPF}spine")
        spine: list[str] = []
        if spine_el is not None:
            for itemref in spine_el.iter(f"{NS_OPF}itemref"):
                href, _, _ = manifest.get(itemref.get("idref") or "", ("", "", ""))
                spine.append(_resolve(opf_path, href))
        # Секция → её номер в spine: так foliate нумерует book.sections.
        index_of = {href: i for i, href in enumerate(spine) if href}

        nav_path = ""
        ncx_path = ""
        for href, media, props in manifest.values():
            if "nav" in props.split() and not nav_path:
                nav_path = _resolve(opf_path, href)
            if media == "application/x-dtbncx+xml" and not ncx_path:
                ncx_path = _resolve(opf_path, href)
        if not ncx_path and spine_el is not None:
            toc_id = spine_el.get("toc") or ""
            if toc_id in manifest:
                ncx_path = _resolve(opf_path, manifest[toc_id][0])

        items: list[dict] = []
        if nav_path:
            try:
                items = _parse_nav(fromstring(zf.read(nav_path)), nav_path)
            except (KeyError, ParseError, DefusedXmlException) as e:
                log.warning("nav %s не разобран: %s", nav_path, e)
        if not items and ncx_path:
            try:
                items = _parse_ncx(fromstring(zf.read(ncx_path)), ncx_path)
            except (KeyError, ParseError, DefusedXmlException) as e:
                log.warning("ncx %s не разобран: %s", ncx_path, e)

        if items:
            # Ссылку на главу у источника читаем один раз на файл секции:
            # пунктов оглавления бывает больше, чем файлов (подглавы).
            url_cache: dict[str, str] = {}
            for it in items:
                base = (it.get("href") or "").split("#", 1)[0]
                it["index"] = index_of.get(base, -1)
                if base not in url_cache:
                    url_cache[base] = _chapter_url(zf, base) if base else ""
                it["url"] = url_cache[base]
            return items

        # Ни nav, ни NCX (или они пустые): оглавление из самого spine —
        # заголовки берём из файлов секций. Молча отдавать пустой список
        # нельзя: страница книги показала бы «глав нет» у книги с главами.
        log.info("%s: оглавления нет ни в nav, ни в NCX — собираю по spine", path.name)
        for i, href in enumerate(spine[:MAX_SPINE_PROBE]):
            if not href:
                continue
            items.append(
                {
                    "label": _spine_title(zf, href) or f"Раздел {i + 1}",
                    "href": href,
                    "index": i,
                    "level": 0,
                    "url": _chapter_url(zf, href),
                }
            )
        return items


# ---------------------------------------------------------------------------- FB2


def _fb2_toc(path: Path) -> list[dict]:
    root = parse(path).getroot()
    bodies = [el for el in root if _local(el.tag) == "body"]
    if not bodies:
        return []
    items: list[dict] = []
    for i, child in enumerate(bodies[0]):
        if len(items) >= MAX_ITEMS:
            break
        title_el = next((s for s in child if _local(s.tag) == "title"), None)
        if title_el is not None:
            label = _text_of(title_el)
        elif _local(child.tag) == "title":
            label = _text_of(child)
        else:
            label = ""
        items.append(
            {
                "label": label or f"Раздел {i + 1}",
                # У FB2 foliate адресует секции номером (book.toc href = index).
                "href": str(i),
                "index": i,
                "level": 0,
                "url": "",
            }
        )
        # Вложенные главы. Нумерация k — по ТЕМ вложенным секциям, у которых
        # есть заголовок: ровно их собирает fb2.js (':scope > section > .title'),
        # и по этому же k строит href вида "<секция>#<номер заголовка>".
        # Считать k по всем подсекциям подряд — значит уехать на чужую главу.
        k = 0
        for sub in child:
            if len(items) >= MAX_ITEMS:
                break
            if _local(sub.tag) != "section":
                continue
            sub_title = next((t for t in sub if _local(t.tag) == "title"), None)
            if sub_title is None:
                continue
            items.append(
                {
                    "label": _text_of(sub_title) or f"Глава {k + 1}",
                    "href": f"{i}#{k}",
                    "index": i,
                    "level": 1,
                    "url": "",
                }
            )
            k += 1
    return items


# -------------------------------------------------------------------------- кэш

# Разбор EPUB на 181 главу — десятки миллисекунд, но страницу книги открывают
# часто. Ключ включает mtime и размер: книга дорастает новыми главами, и тогда
# запись должна протухнуть сама.
_cache: dict[int, tuple[float, int, list[dict]]] = {}
_cache_lock = threading.Lock()
MAX_CACHED = 200


def extract_toc(path: Path, fmt: str) -> list[dict]:
    """Оглавление файла книги. Неизвестный формат/битый файл → пустой список."""
    fmt = (fmt or path.suffix.lstrip(".")).lower()
    try:
        if fmt == "epub" or zipfile.is_zipfile(path):
            return _epub_toc(path)
        if fmt == "fb2":
            return _fb2_toc(path)
    except (OSError, ParseError, DefusedXmlException,
            zipfile.BadZipFile, KeyError) as e:
        # Пустой список — это «оглавления нет», а тут ДРУГОЕ: файл не разобран.
        # Молчать нельзя, иначе битая книга неотличима от книги без оглавления.
        log.warning("оглавление %s (%s) не разобрано: %s", path, fmt, e)
        return []
    log.info("формат %s оглавление не поддерживает (%s)", fmt, path.name)
    return []


def toc_for(work_id: int, path: Path, fmt: str) -> list[dict]:
    """То же с кэшем по (mtime, size) файла."""
    try:
        st = path.stat()
    except OSError as e:
        log.warning("нет файла книги %s: %s", path, e)
        return []
    key = (st.st_mtime, st.st_size)
    with _cache_lock:
        hit = _cache.get(work_id)
        if hit and (hit[0], hit[1]) == key:
            return hit[2]
    items = extract_toc(path, fmt)
    with _cache_lock:
        if len(_cache) >= MAX_CACHED:
            _cache.pop(next(iter(_cache)), None)
        _cache[work_id] = (key[0], key[1], items)
    return items
