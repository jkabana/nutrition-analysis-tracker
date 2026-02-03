from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.analytics.plateau import detect_plateau
from app.auth import get_current_user
from app.db import supabase

router = APIRouter()

class PlateauRequest(BaseModel):
    window_days: int = 14


@router.post("/analytics/plateau")
def plateau(payload: PlateauRequest, user=Depends(get_current_user)):
    user_id = user.id

    # ⚠️ Update these names to match your schema if different
    rows = (
        supabase.table("weight_logs")
        .select("logged_at, weight")
        .eq("user_id", user_id)
        .order("logged_at")
        .execute()
        .data
    )

    if not rows:
        raise HTTPException(status_code=400, detail="No weigh-ins found for this user")

    weighins = [
        {"date": r["logged_at"], "weight": r["weight"]}
        for r in rows
        if r.get("weight") is not None and r.get("logged_at") is not None
    ]

    result = detect_plateau(
        weighins,
        window_days=payload.window_days,
        # Optional: add sodium_series later
        sodium_series=None,
    )

    return {"plateau": result}

