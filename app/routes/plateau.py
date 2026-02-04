import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.analytics.plateau import detect_plateau

router = APIRouter()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
APP_USER_ID = os.environ["APP_USER_ID"]  # single-user MVP


def sb_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


class PlateauRequest(BaseModel):
    window_days: int = 14


@router.post("/analytics/plateau")
async def plateau(payload: PlateauRequest):
    # Pull enough rows to cover the window; we’ll filter again defensively
    end_date = date.today()
    start_date = end_date - timedelta(days=payload.window_days - 1)

    # ⚠️ Update these names if your schema differs
    TABLE = "weigh_ins"
    DATE_COL = "weigh_date"
    WEIGHT_COL = "weight_lbs"

    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"
    params = {
        "select": f"{DATE_COL},{WEIGHT_COL}",
        "user_id": f"eq.{APP_USER_ID}",
        "order": f"{DATE_COL}.asc",
        # If your DATE_COL is a date (not timestamp), keep this as-is.
        # If it's a timestamp, this still works with ISO format.
        DATE_COL: f"gte.{start_date.isoformat()}",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=sb_headers(), params=params)

    if r.status_code >= 300:
        raise HTTPException(
            status_code=500,
            detail=f"Supabase fetch failed: {r.status_code} {r.text}",
        )

    rows = r.json()
    if not rows:
        raise HTTPException(status_code=400, detail="No weigh-ins found in the requested window")

    weighins = [
        {"date": row.get(DATE_COL), "weight": row.get(WEIGHT_COL)}
        for row in rows
        if row.get(DATE_COL) is not None and row.get(WEIGHT_COL) is not None
    ]

    result = detect_plateau(
        weighins,
        window_days=payload.window_days,
        min_weighins=10,
        sodium_series=None,
    )

    return {"plateau": result}

