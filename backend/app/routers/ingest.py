"""Роутер скачивания: вставил ссылку -> скачали -> добавили в библиотеку и Calibre."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session

from ...accounts import monitor, store
from ...downloaders import chain
from ...downloaders.base import DownloaderError
from .. import ingestjob, webjob
from ..db.models import Work
from ..db.session import engine, get_session
from ..services import register_download

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = logging.getLogger("reader.ingest")


class IngestIn(BaseModel):
    query: str
    # true — не ждать скачивания: ответ сразу (202, job_id), итог опросом
    # GET /api/ingest/jobs/{job_id}. Большая книга качается дольше nginx
    # proxy_read_timeout (300 с): «Червь», 311 глав, ~7 мин — пользователь видел
    # 504, хотя книга докачивалась (serg/tasks#887). false — прежний синхронный
    # контракт для внешних клиентов.
    background: bool = False


def _do_ingest(q: str, session: Session) -> Work:
    """Скачать по ссылке/названию, зарегистрировать и поставить на отслеживание.

    Старт и итог пишутся в лог (journald → Alloy → Loki): без этих строк в
    Grafana не видно даже того, что книга начала качаться (serg/tasks#888).
    """
    t0 = time.monotonic()
    log.info("скачивание начато: %s", q)
    try:
        # Подставить креды аккаунта для домена (если есть) — для закрытого/18+.
        creds = store.creds_for_host(session, _host(q)) if chain.is_url(q) else None
        result = chain.fetch(q, creds=creds)
        work = register_download(result, session)
        # Поставить фик на отслеживание обновлений. Метрику подписки задаёт адаптер,
        # если она НЕ равна числу глав: у документации Python это номер версии
        # (spec.reader.python-docs). Иначе подписка завелась бы с числом секций
        # файла в «версионных» единицах и требовала лишней перекачки.
        if work.source_url:
            metric = (result.extra or {}).get("update_metric") or work.chapters_count
            monitor.add_monitor(session, work.source_url, work.id, metric)
    except Exception as e:
        log.warning(
            "скачивание не удалось за %.1f с: %s — %s: %s",
            time.monotonic() - t0, q, type(e).__name__, e,
        )
        raise
    log.info(
        "скачано за %.1f с: %s → work=%s «%s», глав %s",
        time.monotonic() - t0, q, work.id, work.title, work.chapters_count,
    )
    return work


def _ingest_in_own_session(q: str) -> dict:
    """Фоновый вариант: у потока своя сессия, наружу — краткое описание книги."""
    with Session(engine) as session:
        work = _do_ingest(q, session)
        return {
            "id": work.id,
            "title": work.title,
            "author": work.author,
            "chapters": work.chapters_count,
        }


@router.post("", response_model=None)
def ingest(
    body: IngestIn, response: Response, session: Session = Depends(get_session)
):
    """Скачать произведение по ссылке и зарегистрировать в библиотеке.

    Синхронный режим FastAPI выполнит в threadpool, поэтому блокирующий
    subprocess FanFicFare не стопорит event loop. Фоновый (background=true)
    отвечает сразу и не упирается в таймаут nginx.
    """
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "пустой запрос")
    if body.background:
        response.status_code = 202
        return ingestjob.start(q, lambda: _ingest_in_own_session(q))
    try:
        return _do_ingest(q, session)
    except DownloaderError as e:
        raise HTTPException(422, str(e))


@router.get("/jobs/{job_id}")
def ingest_job(job_id: str) -> dict:
    """Статус фонового добавления: queued | running | done | error."""
    st = ingestjob.get(job_id)
    if st is None:
        raise HTTPException(
            404,
            "задание не найдено — сервис мог перезапуститься; проверьте библиотеку",
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
