"""Роутер прогресса чтения: получить/сохранить позицию.

Прогресс хранится как ratio (0..1, совместимо с ReadEra) + точный locator для
foliate-js. На этапе 3 этот же прогресс реконсилится с бэкапом ReadEra.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db.models import PositionHistory, Progress, Work, utcnow
from ..db.session import get_session

router = APIRouter(prefix="/api/progress", tags=["progress"])


# Сколько прошлых позиций держим на книгу. Список переходов — рабочий
# инструмент, а не архив: глубже нескольких десятков в него не заглядывают,
# зато каждая запись стоит строки в БД на каждую книгу библиотеки.
MAX_HISTORY = 50
# Насколько должна измениться доля, чтобы сохранение считалось ПРЫЖКОМ, а не
# обычным чтением. 0.005 книги — это примерно одна глава у фанфика на 200 глав:
# листание страниц историю не засоряет, а «вкладка со страницей 1 затёрла главу
# 100» попадает в неё гарантированно.
JUMP_RATIO = 0.005


def _push_history(
    session: Session,
    prog: Progress,
    *,
    chapter: str = "",
    reason: str = "jump",
) -> None:
    """Сохранить ТЕКУЩУЮ позицию в историю (до того, как её перезапишут)."""
    if not (prog.locator or prog.text_anchor or prog.ratio):
        return  # пустая позиция: возвращаться в неё некуда
    last = session.exec(
        select(PositionHistory)
        .where(PositionHistory.work_id == prog.work_id)
        .order_by(PositionHistory.created_at.desc(), PositionHistory.id.desc())
    ).first()
    # Тот же locator подряд — это не новый переход, а повтор.
    if last and last.locator == prog.locator and last.text_anchor == prog.text_anchor:
        return
    session.add(
        PositionHistory(
            work_id=prog.work_id,
            ratio=float(prog.ratio or 0.0),
            locator=prog.locator or "",
            text_anchor=prog.text_anchor or "",
            chapter=chapter,
            reason=reason,
        )
    )
    _trim_history(session, prog.work_id)


def _trim_history(session: Session, work_id: int) -> None:
    """Оставить последние MAX_HISTORY записей книги."""
    rows = session.exec(
        select(PositionHistory)
        .where(PositionHistory.work_id == work_id)
        .order_by(PositionHistory.created_at.desc(), PositionHistory.id.desc())
    ).all()
    for extra in rows[MAX_HISTORY:]:
        session.delete(extra)


class ProgressIn(BaseModel):
    ratio: float = Field(ge=0.0, le=1.0)
    locator: str = ""
    text_anchor: str = ""
    # Название главы на момент сохранения: в Progress не хранится (там только
    # координаты), но нужно истории позиций — подписью к записи.
    chapter: str = ""


class HistoryIn(BaseModel):
    """Снимок позиции, который фронт кладёт в историю ПЕРЕД прыжком."""

    ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    locator: str = ""
    text_anchor: str = ""
    chapter: str = ""
    reason: str = "jump"


def effective_ratio(prog: Progress, chapters_now: int) -> float:
    """Доля прочитанного с поправкой на выросшую книгу.

    Хранимый ratio — доля от объёма НА МОМЕНТ ЧТЕНИЯ. Когда FanFicFare докачал
    новые главы, та же позиция стала меньшей долей книги, но в базе осталось
    прежнее число. Из-за этого дочитанная книга навсегда оставалась «дочитанной»
    (ratio >= 0.98), карточка прятала плашку обновления — и новые главы человек
    просто не видел. Карточка на это и рассчитывала: «докачается глава — ratio
    упадёт ниже порога и плашка вернётся сама», только падать было нечему.

    Пересчёт пропорционален числу глав: приблизительно (главы разного размера),
    но достаточно, чтобы книга перестала считаться дочитанной. Хранимое значение
    не трогаем — оно уезжает в ReadEra как есть; правим только то, что отдаём.
    """
    ratio = float(prog.ratio or 0.0)
    was = int(prog.chapters_at_read or 0)
    now = int(chapters_now or 0)
    if was <= 0 or now <= was:
        return ratio
    return max(0.0, min(1.0, ratio * was / now))


@router.get("")
def all_progress(session: Session = Depends(get_session)) -> dict[int, float]:
    """Все позиции разом {work_id: ratio} — чтобы фронт не делал N запросов на список книг."""
    rows = session.exec(select(Progress)).all()
    chapters = {
        int(w_id): int(cnt or 0)
        for w_id, cnt in session.exec(select(Work.id, Work.chapters_count)).all()
    }
    return {
        int(p.work_id): effective_ratio(p, chapters.get(int(p.work_id), 0))
        for p in rows
    }


@router.get("/{work_id}")
def get_progress(work_id: int, session: Session = Depends(get_session)) -> Progress:
    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    if not prog:
        # Пустой прогресс по умолчанию (книга ещё не открывалась).
        return Progress(work_id=work_id, ratio=0.0, locator="", source="web")
    # Доля — с поправкой на выросшую книгу; locator и якорь возвращаем как есть,
    # позиция восстанавливается по ним, а не по доле.
    work = session.get(Work, work_id)
    prog.ratio = effective_ratio(prog, work.chapters_count if work else 0)
    return prog


@router.put("/{work_id}")
def set_progress(
    work_id: int,
    body: ProgressIn,
    session: Session = Depends(get_session),
) -> Progress:
    work = session.get(Work, work_id)
    if not work:
        raise HTTPException(404, "work not found")

    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    if prog:
        # Позицию вот-вот перезапишут. Если новая далеко от старой — это не
        # чтение, а прыжок (или затирание чужой вкладкой, открытой в начале
        # книги): прежнее место уезжает в историю, чтобы «Назад» в читалке
        # было куда нажимать.
        jumped = abs(float(prog.ratio or 0.0) - float(body.ratio)) >= JUMP_RATIO
        if jumped:
            _push_history(session, prog, chapter=prog.chapter, reason="overwrite")
        prog.ratio = body.ratio
        prog.locator = body.locator
        # Пустой якорь не затираем сохранённым: релокейт без видимого текста
        # (пустая/картиночная страница) не должен стирать рабочий якорь.
        if body.text_anchor:
            prog.text_anchor = body.text_anchor
        # Глава пришла — пишем. Пришла пустая (обложка, титул или
        # релокейт без tocItem) — сохраняем прежнюю подпись ТОЛЬКО пока читают
        # то же место. При прыжке прежняя глава уже не про эту позицию —
        # лучше пустая подпись, чем чужая.
        if body.chapter or jumped:
            prog.chapter = body.chapter
        prog.last_read_time = utcnow()
        prog.chapters_at_read = int(work.chapters_count or 0)
        prog.source = "web"
    else:
        prog = Progress(
            work_id=work_id,
            ratio=body.ratio,
            locator=body.locator,
            text_anchor=body.text_anchor,
            chapter=body.chapter,
            chapters_at_read=int(work.chapters_count or 0),
            source="web",
        )
        session.add(prog)

    # Отметим время последней активности на самой работе (для сортировки/sync).
    # Это НЕ «дата выхода новых глав» — для неё есть content_updated_at.
    work.updated_at = utcnow()
    session.add(work)
    session.commit()
    session.refresh(prog)
    return prog


@router.get("/{work_id}/history")
def get_history(
    work_id: int, session: Session = Depends(get_session)
) -> list[PositionHistory]:
    """Прошлые позиции книги, новые первыми."""
    return list(
        session.exec(
            select(PositionHistory)
            .where(PositionHistory.work_id == work_id)
            .order_by(PositionHistory.created_at.desc(), PositionHistory.id.desc())
            .limit(MAX_HISTORY)
        ).all()
    )


@router.post("/{work_id}/history")
def add_history(
    work_id: int,
    body: HistoryIn,
    session: Session = Depends(get_session),
) -> PositionHistory:
    """Положить позицию в историю (читалка зовёт это перед прыжком)."""
    if not session.get(Work, work_id):
        raise HTTPException(404, "work not found")
    row = PositionHistory(
        work_id=work_id,
        ratio=body.ratio,
        locator=body.locator,
        text_anchor=body.text_anchor,
        chapter=body.chapter,
        reason=body.reason or "jump",
    )
    session.add(row)
    _trim_history(session, work_id)
    session.commit()
    session.refresh(row)
    return row


@router.delete("/{work_id}/history")
def clear_history(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Очистить историю переходов книги."""
    rows = session.exec(
        select(PositionHistory).where(PositionHistory.work_id == work_id)
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return {"deleted": len(rows)}
