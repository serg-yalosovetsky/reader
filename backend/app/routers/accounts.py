"""Роутер аккаунтов и мониторинга обновлений."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ...accounts import monitor, store
from ..db.session import get_session

router = APIRouter(prefix="/api", tags=["accounts"])


class AccountIn(BaseModel):
    site: str       # ficbook | fanfics | ao3 | ffn
    username: str
    password: str


class MonitorIn(BaseModel):
    url: str


# ---- аккаунты ----
@router.get("/accounts")
def accounts(session: Session = Depends(get_session)) -> list[dict]:
    return store.list_accounts(session)


@router.post("/accounts")
def add_account(body: AccountIn, session: Session = Depends(get_session)) -> dict:
    if not body.site or not body.username:
        raise HTTPException(400, "нужны site и username")
    acc = store.upsert_account(session, body.site.strip(), body.username.strip(), body.password)
    return {"id": acc.id, "site": acc.site, "username": acc.username}


@router.delete("/accounts/{account_id}")
def del_account(account_id: int, session: Session = Depends(get_session)) -> dict:
    return {"deleted": store.delete_account(session, account_id)}


# ---- мониторинг ----
@router.get("/monitored")
def monitored(session: Session = Depends(get_session)) -> list[dict]:
    return monitor.list_monitored(session)


@router.post("/monitored")
def add_monitored(body: MonitorIn, session: Session = Depends(get_session)) -> dict:
    if not body.url.strip():
        raise HTTPException(400, "нужен url")
    m = monitor.add_monitor(session, body.url.strip())
    return {"id": m.id, "source_url": m.source_url}


@router.post("/monitored/check")
def check_now(session: Session = Depends(get_session)) -> dict:
    """Проверить обновления сейчас (синхронно; FastAPI выполнит в threadpool)."""
    return monitor.check_all(session, auto_download=True)
