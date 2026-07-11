"""Дедупликация и нормализация подписок мониторинга (Monitored).

Одна книга легко обрастает несколькими записями Monitored: разные зеркала
(ficbook + author.today), либо URL самого фика и URL отдельной главы. Держим
ОДНУ каноническую запись на work_id (и одну на «сиротский» source_url без
work_id), сливая в неё накопленное состояние.

Заодно гасим ЛОЖНЫЙ флаг обновления: ficbook-лента метит has_update при любой
активности автора (новый арт, правка описания), не только при новых главах. Флаг
оставляем истинным лишь когда на сайте реально больше глав, чем учтено
(work.chapters_count > last_seen_chapters); иначе это шум и он снимается.
"""

from __future__ import annotations

from sqlmodel import Session, select

from ..app.db.models import Monitored, Work


def _merge_group(group: list[Monitored]) -> Monitored:
    """Схлопнуть дубли в одну запись: канон — с наибольшим last_seen_chapters;
    last_seen = max по группе, has_update = OR (реальность флага уточняется позже
    по числу глав)."""
    group.sort(key=lambda m: m.last_seen_chapters or 0, reverse=True)
    keep = group[0]
    keep.last_seen_chapters = max((m.last_seen_chapters or 0) for m in group)
    keep.has_update = any(m.has_update for m in group)
    return keep


def _collapse_group(session: Session, group: list[Monitored]) -> Monitored:
    """Слить группу дублей: оставить каноническую запись, удалить остальные.
    Возвращает канон (has_update у него может ещё уточниться по числу глав)."""
    keep = _merge_group(group)
    session.add(keep)
    for dup in group[1:]:
        session.delete(dup)
    return keep


def dedup_monitored(session: Session) -> dict:
    """Свести дубли Monitored к одной записи на work_id / source_url и снять
    ложные has_update. Возвращает {'removed': N, 'flags_fixed': M}."""
    by_work: dict[int, list[Monitored]] = {}
    orphans: dict[str, list[Monitored]] = {}
    for m in session.exec(select(Monitored)).all():
        if m.work_id:
            by_work.setdefault(m.work_id, []).append(m)
        else:
            orphans.setdefault((m.source_url or "").strip(), []).append(m)

    removed = 0
    flags_fixed = 0

    # Дубли с известным work_id — сливаем и нормализуем флаг по реальным главам.
    for wid, group in by_work.items():
        keep = _collapse_group(session, group)
        removed += len(group) - 1
        work = session.get(Work, wid)
        if (
            work
            and keep.has_update
            and (work.chapters_count or 0) <= (keep.last_seen_chapters or 0)
        ):
            keep.has_update = False
            flags_fixed += 1
            session.add(keep)

    # Дубли без work_id — только по source_url (главы сверить не с чем).
    for url, group in orphans.items():
        _collapse_group(session, group)
        removed += len(group) - 1

    session.commit()
    return {"removed": removed, "flags_fixed": flags_fixed}
