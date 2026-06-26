"""Роутер аккаунтов и мониторинга обновлений."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ...accounts import check_job, monitor, store
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
    # Явное ручное добавление снимает книгу с чёрного списка.
    from ..blacklist import unblock as _bl_unblock
    _bl_unblock(session, source_url=body.url.strip())
    m = monitor.add_monitor(session, body.url.strip())
    if m is None:
        raise HTTPException(409, "книга в чёрном списке")
    return {"id": m.id, "source_url": m.source_url}


@router.post("/monitored/check")
def check_now() -> dict:
    """Запустить проверку обновлений в фоне и сразу вернуть управление.

    Раньше проверка шла синхронно и при 38 фиклах вылезала за nginx-таймаут (60s)
    → 504. Теперь стартуем фоновый поток и отдаём {status}; результат фронт
    забирает поллингом GET /api/monitored/check/status."""
    return check_job.start("manual")


@router.get("/monitored/check/status")
def check_status() -> dict:
    """Статус фоновой проверки: idle | running | done | error (+ result/error)."""
    return check_job.state()


@router.post("/monitored/check/{work_id}")
def check_one_now(work_id: int, session: Session = Depends(get_session)) -> dict:
    """Проверить обновления для одной книги и немедленно вернуть результат."""
    return monitor.check_one(session, work_id)
