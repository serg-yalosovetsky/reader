"""Роутер скачивания: вставил ссылку -> скачали -> добавили в библиотеку и Calibre."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session

from ...downloaders import chain
from ...downloaders.base import DownloaderError
from .. import ingest_service, ingestjob, webjob
from ..db.session import get_session

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class IngestIn(BaseModel):
    query: str
    # true — не ждать скачивания: ответ сразу (202, job_id), итог опросом
    # GET /api/ingest/jobs/{job_id}. Большая книга качается дольше nginx
    # proxy_read_timeout (300 с): «Червь», 311 глав, ~7 мин — пользователь видел
    # 504, хотя книга докачивалась (serg/tasks#887). false — прежний синхронный
    # контракт для внешних клиентов.
    background: bool = False


@router.post("", response_model=None)
def ingest(
    body: IngestIn, response: Response, session: Session = Depends(get_session)
):
    """Скачать произведение по ссылке и зарегистрировать в библиотеке.

    background=true — задание в очередь (таблица ingest_job), ответ сразу 202;
    очередь переживает рестарт сервиса (serg/tasks#892). Синхронный режим
    FastAPI выполнит в threadpool — прежний контракт для внешних клиентов.
    """
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "пустой запрос")
    if body.background:
        try:
            snap = ingestjob.enqueue(q)
        except ingestjob.QueueFull as e:
            raise HTTPException(429, str(e)) from e
        response.status_code = 202
        return snap
    try:
        return ingest_service.do_ingest(q, session)
    except DownloaderError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/jobs/{job_id}")
def ingest_job(job_id: str) -> dict:
    """Статус фонового добавления: queued | running | done | error."""
    st = ingestjob.get(job_id)
    if st is None:
        raise HTTPException(
            404, "задание не найдено — возможно, оно старше недели и уже удалено"
        )
    return st


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
