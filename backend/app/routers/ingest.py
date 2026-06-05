"""Роутер скачивания: вставил ссылку -> скачали -> добавили в библиотеку и Calibre."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ...accounts import monitor, store
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
    # Подставить креды аккаунта для домена (если есть) — для закрытого/18+.
    creds = store.creds_for_host(session, _host(q)) if chain.is_url(q) else None
    try:
        result = chain.fetch(q, creds=creds)
    except DownloaderError as e:
        raise HTTPException(422, str(e))
    work = register_download(result, session)
    # Поставить фик на отслеживание обновлений.
    if work.source_url:
        monitor.add_monitor(session, work.source_url, work.id, work.chapters_count)
    return work


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""
