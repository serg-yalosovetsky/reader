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


@router.get("")
def all_progress(session: Session = Depends(get_session)) -> dict[int, float]:
    """Все позиции разом {work_id: ratio} — чтобы фронт не делал N запросов на список книг."""
    rows = session.exec(select(Progress)).all()
    return {int(p.work_id): float(p.ratio or 0.0) for p in rows}


@router.get("/{work_id}")
def get_progress(work_id: int, session: Session = Depends(get_session)) -> Progress:
    prog = session.exec(select(Progress).where(Progress.work_id == work_id)).first()
    if not prog:
        # Пустой прогресс по умолчанию (книга ещё не открывалась).
        return Progress(work_id=work_id, ratio=0.0, locator="", source="web")
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
        prog.source = "web"
    else:
        prog = Progress(
            work_id=work_id,
            ratio=body.ratio,
            locator=body.locator,
            text_anchor=body.text_anchor,
            source="web",
        )
        session.add(prog)

    # Отметим время последнего чтения на самой работе (для сортировки/sync).
    work.updated_at = utcnow()
    session.add(work)
    session.commit()
    session.refresh(prog)
    return prog
