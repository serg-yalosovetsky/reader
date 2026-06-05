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
from .routers import calibre, ingest, library, progress, reader, readera


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Reader — фанфики и Calibre", lifespan=lifespan)

app.include_router(library.router)
app.include_router(reader.router)
app.include_router(progress.router)
app.include_router(ingest.router)
app.include_router(calibre.router)
app.include_router(readera.router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# Раздача SPA-фронтенда (foliate-js + UI темы ReadEra). Должна идти последней,
# чтобы не перехватывать /api/*. html=True отдаёт index.html на корень.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
