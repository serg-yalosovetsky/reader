"""Роутер скачивания: вставил ссылку -> скачали -> добавили в библиотеку и Calibre."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ...accounts import monitor, store
from ...downloaders import chain
from ...downloaders.base import DownloaderError
from .. import webjob
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
    # Поставить фик на отслеживание обновлений. Метрику подписки задаёт адаптер,
    # если она НЕ равна числу глав: у документации Python это номер версии
    # (spec.reader.python-docs). Иначе подписка завелась бы с числом секций
    # файла в «версионных» единицах и требовала лишней перекачки.
    if work.source_url:
        metric = (result.extra or {}).get("update_metric") or work.chapters_count
        monitor.add_monitor(session, work.source_url, work.id, metric)
    return work


# ---- сборка книги из веб-статей (несколько ссылок → одна книга) ----


class WebIn(BaseModel):
    urls: list[str]
    title: str = ""
    author: str = ""


class DiscoverIn(BaseModel):
    url: str
    title: str = ""
    author: str = ""


@router.post("/web")
def ingest_web(body: WebIn) -> dict:
    """Собрать одну книгу из нескольких статей: ссылка = глава, картинки внутрь.

    Работа идёт в фоне (десятки страниц с картинками не укладываются в
    nginx proxy_read_timeout) — статус забирать поллингом GET /api/ingest/web/status.
    """
    urls = [u.strip() for u in (body.urls or []) if (u or "").strip()]
    if not urls:
        raise HTTPException(400, "не передано ни одной ссылки")
    bad = [u for u in urls if not chain.is_url(u)]
    if bad:
        raise HTTPException(400, f"это не ссылки: {', '.join(bad[:3])}")
    return webjob.start_build(urls, title=body.title, author=body.author)


@router.post("/web/auto")
def ingest_web_auto(body: DiscoverIn) -> dict:
    """Одна ссылка → книга: сами находим остальные части серии и собираем.

    Точка для «кинул ссылку и забыл» (скилл Алисы, шэр с телефона). Прогресс —
    там же, GET /api/ingest/web/status (stage: discover → download).
    """
    url = (body.url or "").strip()
    if not chain.is_url(url):
        raise HTTPException(400, "нужна http(s)-ссылка")
    return webjob.start_auto(url, title=body.title, author=body.author)


@router.post("/web/discover")
def ingest_web_discover(body: DiscoverIn) -> dict:
    """Найти остальные части серии по ссылке на одну из них (в фоне)."""
    url = (body.url or "").strip()
    if not chain.is_url(url):
        raise HTTPException(400, "нужна http(s)-ссылка")
    return webjob.start_discover(url)


@router.get("/web/status")
def ingest_web_status() -> dict:
    """Статус фоновой задачи: idle | running | done | error (+progress/result)."""
    return webjob.state()


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or ""
