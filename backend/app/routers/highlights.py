"""Роутер подсветок/цитат: список/создание/удаление по произведению.

Зеркало bookmarks.py, плюс поля text (выделенный фрагмент) и color. Много на
work_id. Авторизацию гейтит nginx (auth_request → vps-sso) — здесь её нет намеренно.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db.models import Highlight, Work
from ..db.session import get_session

router = APIRouter(prefix="/api/highlights", tags=["highlights"])


class HighlightIn(BaseModel):
    ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    locator: str = ""
    text: str = ""
    color: str = "yellow"


@router.get("/{work_id}")
def list_highlights(work_id: int, session: Session = Depends(get_session)) -> list[Highlight]:
    rows = session.exec(
        select(Highlight).where(Highlight.work_id == work_id).order_by(Highlight.ratio)
    ).all()
    return list(rows)


@router.post("/{work_id}")
def add_highlight(
    work_id: int,
    body: HighlightIn,
    session: Session = Depends(get_session),
) -> Highlight:
    if not session.get(Work, work_id):
        raise HTTPException(404, "work not found")
    hl = Highlight(
        work_id=work_id,
        ratio=body.ratio,
        locator=body.locator,
        text=body.text,
        color=body.color,
    )
    session.add(hl)
    session.commit()
    session.refresh(hl)
    return hl


@router.delete("/id/{highlight_id}")
def delete_highlight(highlight_id: int, session: Session = Depends(get_session)) -> dict:
    hl = session.get(Highlight, highlight_id)
    if hl:
        session.delete(hl)
        session.commit()
    return {"deleted": bool(hl)}
