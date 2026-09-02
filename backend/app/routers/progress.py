"""Роутер прогресса чтения: получить/сохранить позицию.

Прогресс хранится как ratio (0..1, совместимо с ReadEra) + точный locator для
foliate-js. На этапе 3 этот же прогресс реконсилится с бэкапом ReadEra.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db.models import Progress, Work, utcnow
from ..db.session import get_session

router = APIRouter(prefix="/api/progress", tags=["progress"])


class ProgressIn(BaseModel):
    ratio: float = Field(ge=0.0, le=1.0)
    locator: str = ""
    text_anchor: str = ""


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
        prog.ratio = body.ratio
        prog.locator = body.locator
        # Пустой якорь не затираем сохранённым: релокейт без видимого текста
        # (пустая/картиночная страница) не должен стирать рабочий якорь.
        if body.text_anchor:
            prog.text_anchor = body.text_anchor
        prog.last_read_time = utcnow()
        prog.chapters_at_read = int(work.chapters_count or 0)
        prog.source = "web"
    else:
        prog = Progress(
            work_id=work_id,
            ratio=body.ratio,
            locator=body.locator,
            text_anchor=body.text_anchor,
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
