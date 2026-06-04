"""Роутер скачивания: вставил ссылку -> скачали -> добавили в библиотеку и Calibre."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ...downloaders import chain
from ...downloaders.base import DownloaderError
from ..db.models import Work
from ..db.session import get_session
from ..services import register_download

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class IngestIn(BaseModel):
    query: str


@router.post("")
def ingest(body: IngestIn, session: Session = Depends(get_session)) -> Work:
    """Скачать произведение по ссылке и зарегистрировать в библиотеке.

    Синхронный эндпоинт: FastAPI выполнит его в threadpool, поэтому блокирующий
    subprocess FanFicFare не стопорит event loop.
    """
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "пустой запрос")
    try:
        result = chain.fetch(q)
    except DownloaderError as e:
        raise HTTPException(422, str(e))
    return register_download(result, session)
