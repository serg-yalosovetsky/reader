"""Точка входа FastAPI: инициализация БД, роутеры API, раздача фронтенда.

Запуск (из корня репозитория):
    uvicorn backend.app.main:app --reload --port 8123
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .config import FRONTEND_DIR
from .db.session import init_db
from .routers import article, accounts, bookmarks, calibre, convert, highlights, ingest, library, progress, reader, readera, tts


def _setup_logging() -> None:
    """Привязать логгеры приложения к stdout — иначе их не видно НИГДЕ.

    uvicorn настраивает ТОЛЬКО свои логгеры; root остаётся без хендлеров, и всё,
    что пишет `logging.getLogger("reader.*")`, уходит в никуда (INFO — точно;
    WARNING попадал в lastResort без имени логгера и времени). Проверено
    2026-09-01: за три дня в journal ноль записей от reader.monitor / reader.chain,
    включая ошибки докачки. См. spec.reader.update-pipeline: отказ обязан быть виден.
    """
    log = logging.getLogger("reader")
    if log.handlers:  # идемпотентность: --reload и тесты зовут lifespan повторно
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Reader — фанфики и Calibre", lifespan=lifespan)


# UI-ассеты (html/js/css) — always-revalidate. StaticFiles не слал Cache-Control,
# и мобильные браузеры кешировали старые css/js эвристически (правки не доезжали).
# no-cache = браузер обязан ревалидировать по ETag (быстрый 304, но всегда свежий).
@app.middleware("http")
async def _no_cache_ui(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp

app.include_router(library.router)
app.include_router(reader.router)
app.include_router(convert.router)
app.include_router(progress.router)
app.include_router(bookmarks.router)
app.include_router(highlights.router)
app.include_router(ingest.router)
app.include_router(calibre.router)
app.include_router(readera.router)
app.include_router(accounts.router)
app.include_router(tts.router)
app.include_router(article.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# Раздача SPA-фронтенда (foliate-js + UI темы ReadEra). Должна идти последней,
# чтобы не перехватывать /api/*. html=True сама отдаёт index.html на корень "/",
# а no-cache для "/" ставит middleware _no_cache_ui выше — отдельный роут не нужен.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
