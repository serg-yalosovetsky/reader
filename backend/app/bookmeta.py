"""Извлечение метаданных книги (описание, жанры/метки, статус, рейтинг) из
epub-opf. Источник богат для FanFicFare-сборок (ficbook/ao3/ffn/fanfics): там в
`dc:subject` лежат вперемешку жанры, метки, персонажи, статус и рейтинг, а в
`dc:description` — аннотация. Парсер локальный (без сети) — годится и для разбора
свежескачанной книги при ingest, и для бэкфилла существующих 308 книг.
"""

from __future__ import annotations

import json
import re
import zipfile
from html import unescape
from pathlib import Path

# Служебные dc:subject от FanFicFare, которые не нужны как метки.
_JUNK_SUBJECTS = {"fanfiction", "ориджиналы", "originals"}
_JUNK_PREFIXES = (
    "last update",
    "words:",
    "chapters:",
    "reviews:",
    "favorites:",
    "follows:",
    "language:",
    "published:",
    "updated:",
    "packaged:",
)

# Маркеры статуса (значение dc:subject целиком).
_STATUS_MAP = {
    "in-progress": "в процессе",
    "in progress": "в процессе",
    "incomplete": "в процессе",
    "ongoing": "в процессе",
    "completed": "завершён",
    "complete": "завершён",
}

# Рейтинги (по совпадению subject целиком, регистронезависимо).
_RATINGS = {
    "nc-21",
    "nc-17",
    "r",
    "pg-13",
    "pg",
    "g",
    "explicit",
    "mature",
    "teen",
    "teen and up audiences",
    "general audiences",
    "not rated",
    "18+",
    "16+",
}


def _opf_text(epub_path: str) -> str | None:
    try:
        with zipfile.ZipFile(epub_path) as z:
            opf = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
            if not opf:
                return None
            return z.read(opf).decode("utf-8", "ignore")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def _tag_values(opf: str, tag: str) -> list[str]:
    vals = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", opf, re.S | re.I)
    out = []
    for v in vals:
        v = unescape(re.sub(r"<[^>]+>", "", v)).strip()
        if v:
            out.append(v)
    return out


# Коды жанров FictionBook (fb2) → человекочитаемые русские метки. Покрывают
# частые для фан/лит-РПГ книг коды; неизвестный код показываем «как есть».
_FB2_GENRES = {
    "sf": "Научная фантастика",
    "sf_fantasy": "Фэнтези",
    "sf_action": "Боевая фантастика",
    "sf_epic": "Эпическая фантастика",
    "sf_heroic": "Героическая фантастика",
    "sf_realrpg": "РеалРПГ",
    "sf_litrpg": "ЛитРПГ",
    "sf_postapocalyptic": "Постапокалипсис",
    "sf_horror": "Ужасы",
    "sf_space": "Космическая фантастика",
    "sf_cyberpunk": "Киберпанк",
    "sf_social": "Социальная фантастика",
    "sf_history": "Историческая фантастика",
    "sf_stimpank": "Стимпанк",
    "sf_humor": "Юмористическая фантастика",
    "fantasy": "Фэнтези",
    "fantasy_fight": "Боевое фэнтези",
    "popadanec": "Попаданцы",
    "network_literature": "Сетература",
    "prose_contemporary": "Современная проза",
    "prose_history": "Историческая проза",
    "prose_rus_classic": "Русская классика",
    "detective": "Детектив",
    "thriller": "Триллер",
    "adv_history": "Приключения (история)",
    "adventure": "Приключения",
    "love_sf": "Любовная фантастика",
    "love_detective": "Любовный детектив",
    "love_erotica": "Эротика",
    "love_history": "Исторический любовный роман",
    "love_contemporary": "Современный роман",
    "humor": "Юмор",
    "humor_prose": "Юмористическая проза",
    "child_sf": "Детская фантастика",
    "nonf_biography": "Биография",
    "comp_games": "Компьютерные игры",
    "dramaturgy": "Драматургия",
    "antique": "Античная литература",
    "religion": "Религия",
    "sci_history": "История",
    # частые «голые» коды без префикса sf_
    "realrpg": "РеалРПГ",
    "litrpg": "ЛитРПГ",
    "postapocalyptic": "Постапокалипсис",
    "action": "Боевик",
    "cyberpunk": "Киберпанк",
    "space": "Космос",
    "social": "Социальное",
    "epic": "Эпик",
    "heroic": "Героика",
    "horror": "Ужасы",
    "comp_soft": "Программы",
    "det_action": "Боевой детектив",
    "det_classic": "Классический детектив",
    "det_history": "Исторический детектив",
    "det_political": "Политический детектив",
    "humor_satire": "Сатира",
    "nonf_criticism": "Критика",
    "nonf_publicism": "Публицистика",
    "foreign_sf": "Зарубежная фантастика",
    "foreign_prose": "Зарубежная проза",
    "foreign_adventure": "Зарубежные приключения",
    "foreign_detective": "Зарубежный детектив",
    "foreign_contemporary": "Зарубежная современная проза",
}

# Известные префиксы fb2-кодов — для отката, если полный код не в карте.
_FB2_PREFIXES = (
    "sf_",
    "foreign_",
    "nonf_",
    "det_",
    "prose_",
    "love_",
    "adv_",
    "child_",
    "comp_",
    "sci_",
    "humor_",
)


def _fb2_genre_label(code: str) -> str:
    code = (code or "").strip().lower().replace("-", "_")
    if not code:
        return ""
    if code in _FB2_GENRES:
        return _FB2_GENRES[code]
    # откат: снять известный префикс и попробовать «хвост» (sf_realrpg → realrpg).
    for pre in _FB2_PREFIXES:
        if code.startswith(pre) and code[len(pre) :] in _FB2_GENRES:
            return _FB2_GENRES[code[len(pre) :]]
    return code.replace("_", " ").capitalize()


def _fb2_meta(path: str) -> dict:
    """Метаданные из fb2 (<title-info>): аннотация, жанры (коды→метки), серия."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            data = fh.read(200_000)  # title-info в начале файла
    except OSError:
        return {}
    ti = re.search(r"<title-info>(.*?)</title-info>", data, re.S | re.I)
    scope = ti.group(1) if ti else data

    ann = re.search(r"<annotation>(.*?)</annotation>", scope, re.S | re.I)
    description = ""
    if ann:
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>", ann.group(1), re.S | re.I)
        parts = [unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in paras]
        description = (
            "\n".join(p for p in parts if p)
            or unescape(re.sub(r"<[^>]+>", " ", ann.group(1))).strip()
        )

    # Значение жанра — плоский текст; [^<] не даёт захвату перескочить через
    # пустой самозакрывающийся <genre/> на следующий тег.
    codes = re.findall(r"<genre\b[^>]*>([^<]*)</genre>", scope, re.I)
    genres: list[str] = []
    for code in codes:
        label = _fb2_genre_label(code)
        if label and label not in genres:
            genres.append(label)

    seq = re.search(r"<sequence\b[^>]*name=\"([^\"]+)\"", scope, re.I)
    fandom = unescape(seq.group(1)).strip() if seq else ""

    kw = re.search(r"<keywords>(.*?)</keywords>", scope, re.S | re.I)
    if kw:
        for k in re.split(r"[,;]", unescape(re.sub(r"<[^>]+>", "", kw.group(1)))):
            k = k.strip()
            if k and k not in genres:
                genres.append(k)

    out: dict = {"meta_synced": True}
    if description:
        out["description"] = description
    if genres:
        out["genres"] = json.dumps(genres, ensure_ascii=False)
    if fandom:
        out["fandom"] = fandom
    return out


def extract_meta(path: str | Path, fmt: str = "") -> dict:
    """Единая точка: разобрать метаданные книги (epub-opf или fb2) в поля Work."""
    p = str(path)
    is_fb2 = (fmt or "").lower() == "fb2" or p.lower().endswith(".fb2")
    return _fb2_meta(p) if is_fb2 else extract_epub_meta(p)


def extract_epub_meta(epub_path: str | Path) -> dict:
    """Разобрать opf книги в поля Work. Возвращает пустой dict, если opf нет."""
    opf = _opf_text(str(epub_path))
    if not opf:
        return {}

    desc_list = _tag_values(opf, "dc:description")
    description = desc_list[0] if desc_list else ""

    subjects = _tag_values(opf, "dc:subject")
    rating = ""
    status = ""
    tags: list[str] = []
    for s in subjects:
        low = s.lower().strip()
        if low in _JUNK_SUBJECTS or any(low.startswith(p) for p in _JUNK_PREFIXES):
            continue
        if low in _STATUS_MAP:
            status = _STATUS_MAP[low]
            continue
        if low in _RATINGS:
            # нормализуем к верхнему регистру привычных обозначений
            rating = s.upper() if len(s) <= 6 else s
            continue
        tags.append(s)

    # numWords/numChapters иногда лежат в calibre-meta или subject "Words: N".
    words = 0
    mw = re.search(r"words:\s*([\d\s]+)", opf, re.I)
    if mw:
        words = int(re.sub(r"\D", "", mw.group(1)) or 0)

    out: dict = {"meta_synced": True}
    if description:
        out["description"] = description
    if tags:
        out["genres"] = json.dumps(tags, ensure_ascii=False)
    if rating:
        out["rating"] = rating
    if status:
        out["status"] = status
    if words:
        out["words"] = words
    return out


def apply_meta(work, meta: dict, *, overwrite: bool = False) -> bool:
    """Записать поля meta в Work. По умолчанию не затираем непустые значения.
    Возвращает True, если что-то изменилось."""
    changed = False
    for key, val in meta.items():
        if not hasattr(work, key):
            continue
        cur = getattr(work, key)
        if not overwrite and cur not in ("", 0, False, None):
            continue
        if cur != val:
            setattr(work, key, val)
            changed = True
    return changed
