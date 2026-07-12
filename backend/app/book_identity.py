"""Идентичность книги на разных ресурсах (ficbook / author.today / searchfloor / …).

Автор на разных сайтах записан по-разному: ник или настоящее имя; один и тот же
ник на другом сайте бывает занят → автор берёт другой ник. Поэтому «та же книга»
НЕ сводится к строгому равенству (автор, название) — иначе одна книга плодит
карточки, а книга-ТЁЗКА другого автора ошибочно считается той же (и тащится в
библиотеку с чужой обложкой).

`same_book()` опирается на набор сигналов по убыванию силы:
  название (ворота) → автор (устойчив к ник/имя/транслиту) → серия+номер →
  аннотация → в крайнем случае текст книги.

Политика: одного совпадения названия НЕ достаточно — нужна положительная опора
(автор, аннотация, серия или текст). При неоднозначности книги считаются РАЗНЫМИ
(консервативно — чтобы не импортировать непроверенное).

Модуль чистый: без БД и сети. Дескриптор книги — dict:
  {title, author, series, series_index, annotation|description, chapters, words}
Любое поле может отсутствовать.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

MATCH, CONFLICT, UNKNOWN = "match", "conflict", "unknown"

_QUOTES = r"[\"“”«»'`’]"
# Маркеры номера тома/книги в НАЗВАНИИ — сверяем отдельно (через номер), чтобы
# «Книга 1» и «Книга 3» одной серии не слились. Только многобуквенные, иначе
# затрагиваем настоящие слова названия.
_VOL_MARK = r"(?:томах?|тома?|книг[аи]|част[ьи]|part|book|vol|volume)"


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(_QUOTES, "", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.U)  # пунктуация → пробел
    return re.sub(r"\s+", " ", s).strip()


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _title_key(s: str) -> tuple[str, int | None]:
    """(базовое_название_без_номера_тома, номер|None)."""
    n = _norm(s)
    num = None
    m = re.search(rf"{_VOL_MARK}\s*(\d+)\b", n)
    if m:
        num = int(m.group(1))
    else:
        m2 = re.search(r"\b(\d+)\s*$", n)  # завершающее число («Оракул 2»)
        if m2:
            num = int(m2.group(1))
    base = re.sub(rf"{_VOL_MARK}\s*\d+\b", "", n)
    base = re.sub(r"\b\d+\s*$", "", base)
    return re.sub(r"\s+", " ", base).strip(), num


def title_sim(a: str, b: str) -> float:
    ba, _ = _title_key(a)
    bb, _ = _title_key(b)
    if not ba or not bb:
        return 0.0
    return _ratio(ba, bb)


def _tokens(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) >= 3}


# --- транслит лат→кир (грубый фонетический, для «Brandon» ↔ «Брендон») ---
_L2C = {
    "shch": "щ", "sch": "щ", "yo": "ё", "zh": "ж", "kh": "х", "ts": "ц",
    "ch": "ч", "sh": "ш", "yu": "ю", "ya": "я", "ye": "е", "ph": "ф",
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з",
    "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "f": "ф", "h": "х",
    "c": "к", "y": "ы", "w": "в", "x": "кс", "q": "к",
}


def _lat2cyr(s: str) -> str:
    s = s.lower()
    for k in sorted(_L2C, key=len, reverse=True):
        s = s.replace(k, _L2C[k])
    return s


def _translit_match(a: str, b: str) -> bool:
    ca, cb = _lat2cyr(a), _lat2cyr(b)
    if _ratio(ca, cb) >= 0.82:
        return True
    return bool({w for w in ca.split() if len(w) >= 4} & {w for w in cb.split() if len(w) >= 4})


def author_relation(a: str, b: str) -> str:
    """MATCH | CONFLICT | UNKNOWN. Устойчиво к ник/имя/транслиту/кавычкам."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return UNKNOWN
    if na == nb:
        return MATCH
    # ник вложен в «Фамилия Имя "ник"» (напр. noslnosl ⊂ абрамов владимир noslnosl)
    if len(na) >= 3 and re.search(rf"\b{re.escape(na)}\b", nb):
        return MATCH
    if len(nb) >= 3 and re.search(rf"\b{re.escape(nb)}\b", na):
        return MATCH
    if _tokens(na) & _tokens(nb):  # общий токен-слово (фамилия/ник)
        return MATCH
    if _ratio(na, nb) >= 0.85:  # опечатка/огласовка («Уилан» vs «Уилэн»)
        return MATCH
    if _translit_match(na, nb):
        return MATCH
    return CONFLICT


def annotation_sim(a: str | None, b: str | None) -> float | None:
    """Схожесть аннотаций 0..1 или None, если сравнивать нечего (пусто/коротко)."""
    na, nb = _norm(a or ""), _norm(b or "")
    if len(na) < 40 or len(nb) < 40:
        return None
    r = _ratio(na[:1500], nb[:1500])
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(r, jac)


def series_relation(a: dict, b: dict) -> tuple[str, bool]:
    """(отношение серий, конфликт_номера). Конфликт номера при совпавшей серии,
    но разных series_index → это разные книги одного цикла."""
    sa, sb = _norm(a.get("series", "")), _norm(b.get("series", ""))
    if not sa or not sb:
        rel = UNKNOWN
    elif sa == sb or _ratio(sa, sb) >= 0.9 or (_tokens(sa) & _tokens(sb)):
        rel = MATCH
    else:
        rel = CONFLICT
    ia, ib = a.get("series_index") or 0, b.get("series_index") or 0
    idx_conflict = rel == MATCH and bool(ia) and bool(ib) and ia != ib
    return rel, idx_conflict


def _ann(d: dict) -> str | None:
    return d.get("annotation") or d.get("description")


def _text_fingerprint(ta: str, tb: str, thresh: float = 0.85) -> bool:
    na, nb = _norm(ta)[:3000], _norm(tb)[:3000]
    if len(na) < 500 or len(nb) < 500:
        return False
    return _ratio(na, nb) >= thresh


def same_book(
    a: dict,
    b: dict,
    *,
    get_text_a=None,
    get_text_b=None,
    title_gate: float = 0.90,
    ann_strong: float = 0.75,
    ann_medium: float = 0.50,
) -> bool:
    """Одно ли это произведение. См. модульную docstring про политику сигналов."""
    if title_sim(a.get("title", ""), b.get("title", "")) < title_gate:
        return False

    # Разные тома в самом названии — разные книги. Отсутствие номера трактуем как
    # том 1 («Оракул» = 1-й ⇒ отличается от «Оракул 2»).
    _, na_num = _title_key(a.get("title", ""))
    _, nb_num = _title_key(b.get("title", ""))
    if (na_num is not None or nb_num is not None) and (na_num or 1) != (nb_num or 1):
        return False

    auth = author_relation(a.get("author", ""), b.get("author", ""))
    ser, idx_conflict = series_relation(a, b)
    if idx_conflict:
        return False
    ann = annotation_sim(_ann(a), _ann(b))

    # 1) Автор совпал — та же (если серия явно не конфликтует).
    if auth == MATCH:
        return ser != CONFLICT

    # 2) Автор различается/неизвестен (ник vs имя) — нужна текстовая опора.
    if ann is not None and ann >= ann_strong:
        return True
    if ser == MATCH and ann is not None and ann >= ann_medium:
        return True

    # 3) Аннотации нет/слабая — последняя опора: сверка ТЕКСТА книги (если доступна).
    #    Срабатывает и при CONFLICT, и при UNKNOWN авторе (нет аннотации → смотрим текст).
    if (ann is None or ann < ann_medium) and get_text_a and get_text_b:
        try:
            if _text_fingerprint(get_text_a(), get_text_b()):
                return True
        except Exception:
            pass

    # 4) Ничего не подтвердило → консервативно РАЗНЫЕ.
    return False


def extract_text_sample(path, fmt: str = "", max_chars: int = 4000) -> str:
    """Первые ~max_chars символов ТЕКСТА книги (epub/fb2) — для сверки идентичности
    по содержимому, когда аннотаций нет. Пустая строка при любой ошибке."""
    import zipfile

    if not path:
        return ""
    try:
        p = str(path)
        if (fmt or "").lower() == "fb2" or p.lower().endswith(".fb2"):
            with open(p, encoding="utf-8", errors="ignore") as fh:
                data = fh.read(300_000)
            m = re.search(r"<body\b.*?>(.*?)</body>", data, re.S | re.I)
            txt = re.sub(r"<[^>]+>", " ", m.group(1) if m else data)
            return re.sub(r"\s+", " ", txt).strip()[:max_chars]
        z = zipfile.ZipFile(p)
        parts: list[str] = []
        total = 0
        for n in z.namelist():
            if n.lower().endswith((".xhtml", ".html", ".htm")):
                parts.append(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf-8", "ignore")))
                total += len(parts[-1])
                if total > max_chars * 3:
                    break
        return re.sub(r"\s+", " ", " ".join(parts)).strip()[:max_chars]
    except Exception:  # noqa: BLE001
        return ""


def work_descriptor(w) -> dict:
    """Собрать дескриптор из ORM-Work (или любого объекта с атрибутами)."""
    g = lambda k: getattr(w, k, None)  # noqa: E731
    return {
        "title": g("title") or "",
        "author": g("author") or "",
        "series": g("series") or "",
        "series_index": g("series_index") or 0,
        "annotation": g("description") or "",
        "chapters": g("chapters_count") or 0,
        "words": g("words") or 0,
        "file_path": g("file_path") or "",
        "file_format": g("file_format") or "",
    }
