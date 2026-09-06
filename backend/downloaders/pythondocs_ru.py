"""Официальный русский перевод документации — как готовый кэш переводов.

С 22 августа 2026 документация Python официально доступна по-русски
(`docs.python.org/ru/3/`, объявлено в Python Insider). Перевод делает
сообщество, поэтому он ЧАСТИЧНЫЙ: в русской сборке 3.14.7 переведено около 18
тысяч абзацев из 99 тысяч, остальные остаются английскими.

Отсюда решение: не заводить вторые, русские книги и не учить кнопку «Перевести»
ходить на сайт, а ЗАСЕЯТЬ существующий кэш переводов готовыми парами
«английский абзац → официальный русский». Тогда кнопка «Перевести» на книге
документации отдаёт официальный перевод МГНОВЕННО и бесплатно там, где он есть,
а где его нет — работает прежний путь через модель. Никаких новых сущностей: ни
книг, ни эндпоинтов, ни настроек.

Выравнивание — по ПОЗИЦИИ блока внутри документа: обе сборки Sphinx собирает из
одних и тех же исходников, отличаются только строки. Если число блоков в
документе разошлось (10 документов из 538 на 3.14.7), документ пропускается
целиком: сдвиг на один абзац означал бы, что читатель увидит чужой перевод, и
узнать об этом было бы неоткуда.
"""

from __future__ import annotations

import logging
import warnings
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from sqlmodel import Session, select

from ..app.db.models import SyncState
from ..app.translation_cache import detect_lang, key as cache_key, put as cache_put
from . import pythondocs

log = logging.getLogger("reader.pythondocs.ru")

# Тот же список блоков, что выбирает фронт (frontend/js/translate.js, BLOCKS):
# кэш ищется по тексту ИМЕННО такого элемента, и стоит спискам разойтись, как
# засеянные записи перестанут находиться — молча, без единой ошибки.
BLOCKS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dd", "figcaption"]
TARGET = "ru"
STATE_KEY = "pythondocs_ru_seeded"
BATCH = 500


def _blocks(data: bytes) -> list[str]:
    """Тексты блоков документа в порядке обхода — как `querySelectorAll` у фронта.

    `get_text()` склеивает потомков без разделителя, ровно как `textContent` в
    браузере: иначе хэш абзаца с `<code>` внутри не совпал бы с тем, что пришлёт
    читалка.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(data, "lxml")
    return [el.get_text() for el in soup.find_all(BLOCKS)]


def pairs(master_en: Path, master_ru: Path) -> tuple[list[tuple[str, str]], dict]:
    """Пары «английский абзац → официальный русский» + статистика выравнивания."""
    out: list[tuple[str, str]] = []
    stats = {"docs": 0, "aligned": 0, "mismatch": 0, "pairs": 0, "untranslated": 0}
    with zipfile.ZipFile(master_en) as en, zipfile.ZipFile(master_ru) as ru:
        common = sorted(
            {n for n in en.namelist() if n.endswith(".xhtml")}
            & {n for n in ru.namelist() if n.endswith(".xhtml")}
        )
        stats["docs"] = len(common)
        for name in common:
            a = _blocks(en.read(name))
            b = _blocks(ru.read(name))
            if len(a) != len(b):
                # Структура разошлась — выравнивать по позиции нельзя.
                stats["mismatch"] += 1
                continue
            stats["aligned"] += 1
            for src_text, ru_text in zip(a, b, strict=True):
                src_text = src_text.strip()
                ru_text = ru_text.strip()
                if len(src_text) < 3 or not ru_text:
                    continue
                if src_text == ru_text:
                    stats["untranslated"] += 1  # этот абзац ещё не перевели
                    continue
                # Кэш ищется по паре языков; берём ту же пару, что определит
                # роутер, иначе запись не найдётся.
                if detect_lang(src_text) != "other":
                    continue
                out.append((cache_key(src_text, "other", TARGET), ru_text))
                stats["pairs"] += 1
    return out, stats


def seed(session: Session) -> dict:
    """Скачать русскую сборку той же версии и засеять кэш. Идемпотентно."""
    ver, vint = pythondocs.current_version()
    master_en = pythondocs.fetch_master(ver, vint)
    master_ru = pythondocs.fetch_master(ver, vint, lang="ru")
    rows, stats = pairs(master_en, master_ru)
    for i in range(0, len(rows), BATCH):
        cache_put(rows[i : i + BATCH])
    st = session.exec(select(SyncState).where(SyncState.key == STATE_KEY)).first()
    if st:
        st.value = str(vint)
    else:
        st = SyncState(key=STATE_KEY, value=str(vint))
    session.add(st)
    session.commit()
    stats["version"] = ver
    log.info("pythondocs ru: засеяно %s пар (%s)", stats["pairs"], ver)
    return stats


def maybe_seed(session: Session) -> dict | None:
    """Засеять, если сайт отдаёт версию, для которой ещё не сеяли.

    Вызывается из планового тика монитора: русский перевод догоняет оригинал
    отдельно от него, и после каждого релиза старые пары уже не совпадут с
    новым английским текстом — их надо переснять, а не чинить руками.
    """
    try:
        _, vint = pythondocs.current_version()
    except Exception as e:  # noqa: BLE001 — недоступность сайта не роняет тик
        log.warning("pythondocs ru: версию узнать не удалось: %s", e)
        return None
    st = session.exec(select(SyncState).where(SyncState.key == STATE_KEY)).first()
    if st and st.value == str(vint):
        return None
    return seed(session)
