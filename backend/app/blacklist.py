"""Чёрный список книг.

Книгу, удалённую из библиотеки «крестиком», запоминаем по (название, автор)
и по всем известным source_url, чтобы фиды подписок и монитор её больше не
докачивали и не показывали. Ручное добавление снова (POST /api/monitored)
снимает книгу с чёрного списка.
"""
from __future__ import annotations

from sqlmodel import Session, select

from .db.models import Blacklist
from .services import _norm


def add_entry(session: Session, title: str = "", author: str = "",
              urls: list[str] | None = None) -> None:
    tn, an = _norm(title), _norm(author)
    # no_autoflush: SELECT-проверки посреди цикла иначе преждевременно флашат уже
    # накопленные add(), открывая write-txn в начале и удерживая его на весь цикл
    # SELECT'ов → 'database is locked' при конкуренции с монитором/фидом (READER-5).
    # Набор url уже уникален (множество), дублей внутри вызова нет; один commit()
    # в конце пишет всё разом коротким писателем.
    with session.no_autoflush:
        if tn:
            ex = session.exec(select(Blacklist).where(
                Blacklist.title_norm == tn, Blacklist.author_norm == an,
                Blacklist.source_url == "")).first()
            if not ex:
                session.add(Blacklist(title_norm=tn, author_norm=an, source_url=""))
        for u in {(x or "").strip() for x in (urls or []) if (x or "").strip()}:
            if session.exec(select(Blacklist).where(Blacklist.source_url == u)).first():
                continue
            session.add(Blacklist(title_norm=tn, author_norm=an, source_url=u))
    session.commit()


def is_blacklisted(session: Session, source_url: str = "",
                   title: str = "", author: str = "") -> bool:
    su = (source_url or "").strip()
    if su and session.exec(select(Blacklist).where(Blacklist.source_url == su)).first():
        return True
    tn, an = _norm(title), _norm(author)
    if tn and session.exec(select(Blacklist).where(
            Blacklist.title_norm == tn, Blacklist.author_norm == an)).first():
        return True
    return False


def unblock(session: Session, source_url: str = "",
            title: str = "", author: str = "") -> int:
    n = 0
    su = (source_url or "").strip()
    if su:
        for b in session.exec(select(Blacklist).where(Blacklist.source_url == su)).all():
            session.delete(b); n += 1
    tn, an = _norm(title), _norm(author)
    if tn:
        for b in session.exec(select(Blacklist).where(
                Blacklist.title_norm == tn, Blacklist.author_norm == an)).all():
            session.delete(b); n += 1
    session.commit()
    return n
