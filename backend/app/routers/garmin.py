"""POST /api/garmin/weight — вызывается HA automation при новом взвешивании."""
from __future__ import annotations

import sys
import os
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

router = APIRouter(prefix="/api/garmin", tags=["garmin"])

# HA авторизует запросы своим long-lived токеном
_WEBHOOK_SECRET: str | None = os.environ.get("GARMIN_WEBHOOK_SECRET")

GARMIN_TOKENSTORE = "/opt/garmin/.garmintokens"


def _verify(authorization: Annotated[str | None, Header()] = None) -> None:
    if not _WEBHOOK_SECRET:
        return  # если не настроен — пропускаем (только из LAN)
    if authorization != f"Bearer {_WEBHOOK_SECRET}":
        raise HTTPException(status_code=403, detail="forbidden")


class WeightPayload(BaseModel):
    weight: float           # кг
    date: str | None = None  # YYYY-MM-DD, по умолчанию сегодня


@router.post("/weight")
def push_weight(body: WeightPayload, _: None = Depends(_verify)) -> dict:
    """Загружает вес в Garmin Connect из токен-кеша /opt/garmin/.garmintokens."""
    try:
        sys.path.insert(0, "/opt/garmin")
        from garminconnect import Garmin
        g = Garmin()
        g.login(GARMIN_TOKENSTORE)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Garmin auth failed: {e}")

    target_date = body.date or date.today().isoformat()
    try:
        result = g.add_body_composition(target_date, weight=body.weight)
        upload_id = (result.get("detailedImportResult") or {}).get("uploadId")
        return {"ok": True, "weight": body.weight, "date": target_date, "uploadId": upload_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Garmin upload failed: {e}")
