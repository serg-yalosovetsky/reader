"""Кэш переводов: ключ и доступ к таблице `Translation`.

Отдельный модуль, потому что писателей у кэша ДВОЕ: роутер перевода (кладёт то,
что вернула модель) и сеялка официальной русской документации
(`downloaders/pythondocs_ru.py`, кладёт готовый перевод от сообщества Python).
Ключ обязан считаться одинаково у обоих — разойдись формула, и засеянные записи
просто никогда не найдутся, причём молча: перевод будет работать, но за деньги
и медленно.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .db.models import Translation
from .db.session import engine


def key(text: str, src: str, dst: str) -> str:
    h = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return f"{src}:{dst}:{h}"


def get(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    with Session(engine) as s:
        rows = s.exec(select(Translation).where(Translation.key.in_(keys))).all()
        return {r.key: r.text for r in rows}


def put(pairs: list[tuple[str, str]]) -> None:
    """Записать переводы в кэш, не падая на гонке.

    Два экрана, переведённые одновременно, попадают на общий абзац регулярно.
    Проверка «нет ли уже такого ключа» перед вставкой от этого не спасает: между
    SELECT и INSERT успевает вставить сосед, и UNIQUE на `key` роняет запрос —
    причём ПОСЛЕ того, как инференс уже оплачен, а вместе с трейсом в трекер
    ошибок уезжает текст книги. Поэтому конфликт разрешает сама СУБД.
    """
    if not pairs:
        return
    rows = [{"key": k, "text": t} for k, t in dict(pairs).items()]
    table = Translation.__table__
    dialect = engine.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # незнакомый диалект: вставляем по одной, конфликт гасим откатом
        with Session(engine) as s:
            for row in rows:
                try:
                    s.add(Translation(**row))
                    s.commit()
                except IntegrityError:
                    s.rollback()
        return
    with Session(engine) as s:
        s.exec(_insert(table).values(rows).on_conflict_do_nothing(index_elements=["key"]))
        s.commit()




_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]")


def detect_lang(text: str) -> str:
    """Грубое определение: нужен ответ только на вопрос «это уже русский?».

    Полноценный детектор языка тут был бы лишней зависимостью: решение бинарное,
    а кириллица против латиницы различает интересующий случай надёжно. 'ru' для
    кириллического текста, 'other' для латинского, '' — когда букв почти нет
    (числа, разделители: переводить нечего).
    """
    cyr = len(_CYR.findall(text))
    lat = len(_LAT.findall(text))
    if cyr + lat < 4:
        return ""
    return "ru" if cyr > lat else "other"
