import os
import re
import csv
import io
import hashlib
from datetime import datetime, date
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, UploadFile, File, HTTPException

app = FastAPI()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

APP_USER_ID = os.environ["APP_USER_ID"]  # single-user MVP
ALLOWED_FROM_NUMBER = os.environ.get("ALLOWED_FROM_NUMBER")  # your personal cell, E.164
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/New_York")
TZ = ZoneInfo(APP_TIMEZONE)

TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY")
TELNYX_FROM_NUMBER = os.environ.get("TELNYX_FROM_NUMBER")
TELNYX_TO_NUMBER = os.environ.get("TELNYX_TO_NUMBER")  # your personal cell, E.164

CRON_SECRET = os.environ.get("CRON_SECRET")  # shared secret for cron → backend calls


def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


async def sb_upsert(table: str, payload: dict, on_conflict: str):
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=sb_headers(), json=payload)
    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"Supabase upsert failed: {r.status_code} {r.text}")


def stable_hash(*parts: str) -> str:
    s = "|".join([p or "" for p in parts])
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_weight_lbs(text: str) -> float | None:
    # Accepts: "184", "184.6", "184.6 lb", etc.
    m = re.search(r"(\d{2,3}(?:\.\d{1,2})?)", text)
    if not m:
        return None
    val = float(m.group(1))
    # Simple sanity guardrails
    if val < 70 or val > 500:
        return None
    return val


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhooks/telnyx/inbound-sms")
async def telnyx_inbound_sms(request: Request):
    payload = await request.json()

    # NOTE: For MVP we won’t enforce ED25519 signature verification.
    # Hardening later: verify Telnyx-Signature-Ed25519 + Telnyx-Timestamp.

    try:
        inbound_from = payload["data"]["payload"]["from"]["phone_number"]
        body = payload["data"]["payload"]["text"]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Telnyx payload")

    if ALLOWED_FROM_NUMBER and inbound_from != ALLOWED_FROM_NUMBER:
        # Ignore messages not from your phone
        return {"ok": True, "ignored": True}

    weight = parse_weight_lbs(body)
    if weight is None:
        return {"ok": True, "parsed": False}

    weigh_date = datetime.now(TZ).date()

    await sb_upsert(
        "weigh_ins",
        {
            "user_id": APP_USER_ID,
            "weigh_date": str(weigh_date),
            "weight_lbs": weight,
            "source": "telnyx_sms",
            "raw_message": body,
        },
        on_conflict="user_id,weigh_date",
    )

    return {"ok": True, "parsed": True, "weigh_date": str(weigh_date), "weight_lbs": weight}


@app.post("/imports/mfp-nutrition-summary")
async def import_mfp_nutrition_summary(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload the MyFitnessPal Nutrition Summary CSV")

    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    required = {
        "Date",
        "Meal",
        "Calories",
        "Sodium (mg)",
        "Carbohydrates (g)",
        "Protein (g)",
        "Fat (g)",
    }
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HTTPException(
            status_code=400,
            detail=f"CSV missing required columns. Found: {reader.fieldnames}",
        )

    # Accumulate daily totals while inserting meal rows
    totals_by_day: dict[str, dict[str, float]] = {}
    meal_rows = 0

    for row in reader:
        entry_date = (row.get("Date") or "").strip()
        meal = (row.get("Meal") or "").strip()

        if not entry_date or not meal:
            continue

        def f(key: str) -> float | None:
            v = (row.get(key) or "").strip()
            return float(v) if v else None

        calories = f("Calories")
        sodium_mg = f("Sodium (mg)")
        carbs_g = f("Carbohydrates (g)")
        protein_g = f("Protein (g)")
        fat_g = f("Fat (g)")

        source_row_hash = stable_hash(
            entry_date, meal,
            str(calories or ""), str(sodium_mg or ""),
            str(carbs_g or ""), str(protein_g or ""), str(fat_g or "")
        )

        await sb_upsert(
            "meal_nutrition",
            {
                "user_id": APP_USER_ID,
                "entry_date": entry_date,
                "meal": meal,
                "calories": calories,
                "sodium_mg": sodium_mg,
                "carbs_g": carbs_g,
                "protein_g": protein_g,
                "fat_g": fat_g,
                "source_row_hash": source_row_hash,
                "source": "mfp_nutrition_summary",
            },
            on_conflict="user_id,source_row_hash",
        )
        meal_rows += 1

        day = totals_by_day.setdefault(entry_date, {"calories": 0.0, "sodium_mg": 0.0, "carbs_g": 0.0, "protein_g": 0.0, "fat_g": 0.0})
        if calories is not None: day["calories"] += calories
        if sodium_mg is not None: day["sodium_mg"] += sodium_mg
        if carbs_g is not None: day["carbs_g"] += carbs_g
        if protein_g is not None: day["protein_g"] += protein_g
        if fat_g is not None: day["fat_g"] += fat_g

    # Upsert daily totals
    for day, t in totals_by_day.items():
        await sb_upsert(
            "daily_nutrition",
            {
                "user_id": APP_USER_ID,
                "day": day,
                "calories": t["calories"],
                "sodium_mg": t["sodium_mg"],
                "carbs_g": t["carbs_g"],
                "protein_g": t["protein_g"],
                "fat_g": t["fat_g"],
                "updated_at": datetime.utcnow().isoformat(),
            },
            on_conflict="user_id,day",
        )

    return {"ok": True, "meal_rows_processed": meal_rows, "days_updated": len(totals_by_day)}


@app.post("/jobs/send-daily-prompt")
async def send_daily_prompt(request: Request):
    # Called by Render cron job
    if not CRON_SECRET:
        raise HTTPException(status_code=500, detail="CRON_SECRET not configured")
    if request.headers.get("x-cron-secret") != CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not (TELNYX_API_KEY and TELNYX_FROM_NUMBER and TELNYX_TO_NUMBER):
        raise HTTPException(status_code=500, detail="Telnyx env vars not configured")

    msg = "Good morning — reply with today’s weight (lbs). Example: 184.6"

    # Telnyx Messages API: POST /v2/messages :contentReference[oaicite:3]{index=3}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.telnyx.com/v2/messages",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}", "Content-Type": "application/json"},
            json={"from": TELNYX_FROM_NUMBER, "to": TELNYX_TO_NUMBER, "text": msg},
        )

    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"Telnyx send failed: {r.status_code} {r.text}")

    return {"ok": True}

