"""Точка входа FastAPI: инициализация БД, роутеры API, раздача фронтенда.

Запуск (из корня репозитория):
    uvicorn backend.app.main:app --reload --port 8123
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .config import FRONTEND_DIR
from .db.session import init_db
from .routers import article, accounts, bookmarks, calibre, highlights, ingest, library, progress, reader, readera, tts


@asynccontextmanager
async def lifespan(app: FastAPI):
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


# index.html отдаём с no-cache, чтобы правки UI (списки, кнопки) подхватывались
# без жёсткого обновления браузера. Статика (css/js/vendor) кэшируется штатно.
@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse(
        str(FRONTEND_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# Раздача SPA-фронтенда (foliate-js + UI темы ReadEra). Должна идти последней,
# чтобы не перехватывать /api/*. html=True отдаёт index.html на корень.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
