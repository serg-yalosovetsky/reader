"""Роутер закладок: список/создание/удаление по произведению.

В отличие от Progress (одна строка на work_id), закладок может быть много.
Хранят ratio (сортировка/совместимость) + точный locator для перехода и
необязательную подпись. Авторизацию гейтит nginx (auth_request → vps-sso),
как и у остальных роутеров — здесь её нет намеренно.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db.models import Bookmark, Work
from ..db.session import get_session

router = APIRouter(prefix="/api/bookmarks", tags=["bookmarks"])


class BookmarkIn(BaseModel):
    ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    locator: str = ""
    label: str = ""


@router.get("/{work_id}")
def list_bookmarks(work_id: int, session: Session = Depends(get_session)) -> list[Bookmark]:
    rows = session.exec(
        select(Bookmark).where(Bookmark.work_id == work_id).order_by(Bookmark.ratio)
    ).all()
    return list(rows)


@router.post("/{work_id}")
def add_bookmark(
    work_id: int,
    body: BookmarkIn,
    session: Session = Depends(get_session),
) -> Bookmark:
    if not session.get(Work, work_id):
        raise HTTPException(404, "work not found")
    bm = Bookmark(work_id=work_id, ratio=body.ratio, locator=body.locator, label=body.label)
    session.add(bm)
    session.commit()
    session.refresh(bm)
    return bm


@router.delete("/id/{bookmark_id}")
def delete_bookmark(bookmark_id: int, session: Session = Depends(get_session)) -> dict:
    bm = session.get(Bookmark, bookmark_id)
    if bm:
        session.delete(bm)
        session.commit()
    return {"deleted": bool(bm)}
