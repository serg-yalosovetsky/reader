"""Название и автор загружаемой книги — из самого файла (serg/tasks#1055).

Раньше при ручной загрузке название брали из ИМЕНИ файла, а автора не брали вовсе:
книга с файлом «39794961.fb2» вставала в библиотеку как «39794961» без автора, хотя
`<title-info>` внутри содержал и `<book-title>`, и `<author>`.

Порядок: метаданные формата → титульный экран → (решает вызывающий) имя файла.
Метаданные приоритетнее титульного экрана: первые — данные, второй — вёрстка.

Любой сбой разбора — пустой результат, а не исключение: загрузка книги из-за
нечитаемого заголовка падать не должна.
"""

from __future__ import annotations

import logging
import re
import subprocess
import zipfile
from html import unescape
from pathlib import Path
from posixpath import dirname, join, normpath

log = logging.getLogger("reader.upload")

_FB2_HEAD_BYTES = 400_000  # title-info идёт первым, обложка-base64 — в конце файла
_MIN_ID_DIGITS = 6  # номера каталогов — 6–9 цифр; «1984» и «451» — названия книг
_PDF_TIMEOUT = 15

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")

# Заглушки, которые редакторы и конвертеры пишут вместо настоящих данных.
_PLACEHOLDER_TITLE = re.compile(
    r"^(unknown|untitled|no title|title|book|document|new document|"
    r"без названия|без назви|документ|книга)$",
    re.I,
)
_PLACEHOLDER_TITLE_START = re.compile(r"^microsoft (word|office|powerpoint)\b", re.I)
_TITLE_LOOKS_LIKE_FILE = re.compile(r"\.(docx?|pdf|epub|fb2|txt|indd|rtf|odt)$", re.I)
_PLACEHOLDER_AUTHOR = re.compile(
    r"^(unknown|unknown author|anonymous|неизвестный( автор)?|невідомий( автор)?|"
    r"автор|author|admin|user)$",
    re.I,
)
# «BY J.R.R. TOLKIEN», «By X», «Автор: X». «Author» без двоеточия не трогаем —
# это может быть началом настоящего имени.
_BY_PREFIX = re.compile(r"^\s*(?:by\s+|(?:автор|author)\s*:\s*)", re.I)


def _plain(s: str) -> str:
    return _WS.sub(" ", unescape(_TAG.sub(" ", s or ""))).strip()


def _norm(s: str) -> str:
    """Для сравнения «то же, что имя файла»: только буквы и цифры, без регистра."""
    return re.sub(r"[\W_]+", "", (s or "").lower())


def clean_title(title: str, stem: str = "") -> str:
    """Название или '' — если это пустота или заглушка."""
    t = _plain(title)
    if not t:
        return ""
    # Одни знаки: «---», «...» — не название.
    if not re.search(r"[^\W_]", t):
        return ""
    # Номер из имени файла или каталога (litres art: «39794961») — не название.
    # Короткие числа оставляем: «1984», «451», «2001» — настоящие названия.
    if t.isdigit() and len(t) >= _MIN_ID_DIGITS:
        return ""
    if _PLACEHOLDER_TITLE.match(t) or _PLACEHOLDER_TITLE_START.match(t):
        return ""
    if _TITLE_LOOKS_LIKE_FILE.search(t):
        return ""
    if stem and _norm(t) == _norm(stem):
        return ""
    return t


def clean_author(author: str) -> str:
    """Автор без префикса «BY», либо '' — если это пустота или заглушка."""
    a = _plain(author)
    a = _BY_PREFIX.sub("", a).strip(" ,;")
    if not a or not re.search(r"[^\W\d_]", a):
        return ""
    if _PLACEHOLDER_AUTHOR.match(a):
        return ""
    return a


def _read_head(path: str, limit: int = _FB2_HEAD_BYTES) -> str:
    """Начало fb2 в правильной кодировке (windows-1251 у fb2 не редкость)."""
    with open(path, "rb") as fh:
        raw = fh.read(limit)
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([\w.-]+)[\"']", raw[:200])
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _fb2_author_name(block: str) -> str:
    def part(tag: str) -> str:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.S | re.I)
        return _plain(m.group(1)) if m else ""

    name = " ".join(p for p in (part("first-name"), part("middle-name"), part("last-name")) if p)
    return name or part("nickname")


def _fb2_metadata(path: str) -> tuple[str, str]:
    data = _read_head(path)
    ti = re.search(r"<title-info>(.*?)</title-info>", data, re.S | re.I)
    if not ti:
        return "", ""
    scope = ti.group(1)
    m = re.search(r"<book-title\b[^>]*>(.*?)</book-title>", scope, re.S | re.I)
    title = _plain(m.group(1)) if m else ""
    names = [_fb2_author_name(b) for b in re.findall(r"<author\b[^>]*>(.*?)</author>", scope, re.S | re.I)]
    return title, ", ".join(n for n in names if n)


def _opf(z: zipfile.ZipFile) -> tuple[str, str] | None:
    """(путь к opf, его текст) — путь берём из META-INF/container.xml, иначе по имени."""
    names = z.namelist()
    opf_path = None
    if "META-INF/container.xml" in names:
        c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
        m = re.search(r'full-path="([^"]+)"', c)
        if m:
            opf_path = m.group(1)
    if not opf_path or opf_path not in names:
        opf_path = next((n for n in names if n.lower().endswith(".opf")), None)
    if not opf_path:
        return None
    return opf_path, z.read(opf_path).decode("utf-8", "ignore")


def _epub_metadata(path: str) -> tuple[str, str]:
    with zipfile.ZipFile(path) as z:
        found = _opf(z)
    if not found:
        return "", ""
    opf = found[1]
    m = re.search(r"<dc:title\b[^>]*>(.*?)</dc:title>", opf, re.S | re.I)
    title = _plain(m.group(1)) if m else ""
    authors = []
    for attrs, body in re.findall(r"<dc:creator\b([^>]*)>(.*?)</dc:creator>", opf, re.S | re.I):
        role = re.search(r'role="([^"]*)"', attrs, re.I)
        if role and role.group(1).lower() not in ("aut", "author"):
            continue  # переводчик, иллюстратор, редактор — не автор
        name = _plain(body)
        if name:
            authors.append(name)
    return title, ", ".join(authors)


def _pdf_metadata(path: str) -> tuple[str, str]:
    out = subprocess.run(
        ["pdfinfo", path], capture_output=True, timeout=_PDF_TIMEOUT, check=False
    ).stdout.decode("utf-8", "replace")
    title = author = ""
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k == "Title":
            title = v.strip()
        elif k == "Author":
            author = v.strip()
    return title, author


def _metadata_identity(path: str, fmt: str) -> tuple[str, str]:
    if fmt == "fb2":
        return _fb2_metadata(path)
    if fmt == "epub":
        return _epub_metadata(path)
    if fmt == "pdf":
        return _pdf_metadata(path)
    return "", ""


def _titlepage_from_lines(lines: list[str], *, need_by: bool) -> tuple[str, str]:
    """Название и автор по первым строкам титульного экрана.

    Автор берётся только из строки «BY <имя>» / «Автор: <имя>» — иначе строка
    с издательством или серией стала бы автором. Название — строка прямо перед
    ней. Без строки «BY»: need_by=False отдаёт первую строку как название
    (fb2 `<body><title>` — это и есть заголовок книги), need_by=True — ничего
    (у epub и pdf первая страница часто обложка или копирайт).
    """
    lines = [l for l in (_plain(x) for x in lines[:14]) if l]
    for i, line in enumerate(lines):
        if _BY_PREFIX.match(line) and len(line) < 120:
            title = lines[i - 1] if i > 0 else ""
            return title, _BY_PREFIX.sub("", line)
    if need_by or not lines:
        return "", ""
    return (lines[0] if len(lines[0]) <= 150 else ""), ""


def _fb2_titlepage(path: str) -> tuple[str, str]:
    data = _read_head(path)
    m = re.search(r"<body\b[^>]*>\s*<title>(.*?)</title>", data, re.S | re.I)
    if not m:
        return "", ""
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>", m.group(1), re.S | re.I)
    return _titlepage_from_lines(paras or [m.group(1)], need_by=False)


def _epub_titlepage(path: str) -> tuple[str, str]:
    with zipfile.ZipFile(path) as z:
        found = _opf(z)
        if not found:
            return "", ""
        opf_path, opf = found
        first = re.search(r'<itemref\b[^>]*idref="([^"]+)"', opf, re.I)
        if not first:
            return "", ""
        item = re.search(
            rf'<item\b[^>]*id="{re.escape(first.group(1))}"[^>]*>', opf, re.I
        )
        href = re.search(r'href="([^"]+)"', item.group(0), re.I) if item else None
        if not href:
            return "", ""
        member = normpath(join(dirname(opf_path), href.group(1).split("#")[0]))
        if member not in z.namelist():
            return "", ""
        html = z.read(member).decode("utf-8", "ignore")
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"</(p|div|h\d|li|br)\s*>|<br\s*/?>", "\n", html, flags=re.I)
    return _titlepage_from_lines(_plain_lines(text), need_by=True)


def _plain_lines(text: str) -> list[str]:
    return [_plain(x) for x in text.split("\n")]


def _pdf_titlepage(path: str) -> tuple[str, str]:
    out = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-q", path, "-"],
        capture_output=True, timeout=_PDF_TIMEOUT, check=False,
    ).stdout.decode("utf-8", "replace")
    return _titlepage_from_lines(out.splitlines(), need_by=True)


def _titlepage_identity(path: str, fmt: str) -> tuple[str, str]:
    if fmt == "fb2":
        return _fb2_titlepage(path)
    if fmt == "epub":
        return _epub_titlepage(path)
    if fmt == "pdf":
        return _pdf_titlepage(path)
    return "", ""


def extract_identity(path: str | Path, fmt: str, stem: str = "") -> tuple[str, str]:
    """(название, автор) книги; '' там, где определить не удалось.

    `stem` — имя файла без расширения: название, совпадающее с ним, считается
    заглушкой (иначе номер из имени файла вернулся бы «из метаданных»).
    """
    p = str(path)
    title = author = ""
    try:
        mt, ma = _metadata_identity(p, fmt)
        title, author = clean_title(mt, stem), clean_author(ma)
    except Exception as e:  # noqa: BLE001 — сбой разбора не должен ронять загрузку
        log.warning("метаданные %s не разобраны: %s", Path(p).name, e)
    if not title or not author:
        try:
            pt, pa = _titlepage_identity(p, fmt)
            title = title or clean_title(pt, stem)
            author = author or clean_author(pa)
        except Exception as e:  # noqa: BLE001
            log.warning("титульный экран %s не разобран: %s", Path(p).name, e)
    return title, author
