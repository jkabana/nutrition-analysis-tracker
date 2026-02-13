from __future__ import annotations

import os
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

import asyncpg

router = APIRouter(prefix="/api", tags=["import"])

DATABASE_URL = os.getenv("DATABASE_URL")

BATCH_SIZE = 500

UPSERT_MEAL_SQL = """
with incoming as (
  select *
  from jsonb_to_recordset($1::jsonb) as x(
    entry_date date,
    meal text,
    calories numeric,
    carbs_g numeric,
    fat_g numeric,
    protein_g numeric,
    sodium_mg numeric,
    source text,
    source_row_hash text
  )
)
insert into public.meal_nutrition (
  user_id,
  entry_date,
  meal,
  calories,
  carbs_g,
  fat_g,
  protein_g,
  sodium_mg,
  source,
  source_row_hash
)
select
  $2::uuid as user_id,
  entry_date,
  meal,
  calories,
  carbs_g,
  fat_g,
  protein_g,
  sodium_mg,
  coalesce(source, 'mfp_csv'),
  source_row_hash
from incoming
on conflict (user_id, source_row_hash)
do update set
  calories   = excluded.calories,
  carbs_g    = excluded.carbs_g,
  fat_g      = excluded.fat_g,
  protein_g  = excluded.protein_g,
  sodium_mg  = excluded.sodium_mg,
  entry_date = excluded.entry_date,
  meal       = excluded.meal,
  source     = excluded.source;
"""

def chunk(items: List[Dict[str, Any]], size: int) -> List[List[Dict[str, Any]]]:
    return [items[i:i+size] for i in range(0, len(items), size)]


@router.post("/import/mfp")
async def import_mfp(request: Request, rows: List[Dict[str, Any]]):
    """
    Import MFP-prepared rows into meal_nutrition.

    For now, this endpoint expects the caller to be trusted (Retool).
    We set user_id from an env var to prove the import path works.
    Next step: derive user_id from Supabase JWT.
    """
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not set")

    # TEMP: hardcode user_id via env var so we can confirm imports work end-to-end.
    # Set this in Render as IMPORT_USER_ID (Supabase auth.users.id UUID).
    user_id = os.getenv("IMPORT_USER_ID")
    if not user_id:
        raise HTTPException(status_code=500, detail="IMPORT_USER_ID is not set on the server")

    if not isinstance(rows, list) or len(rows) == 0:
        return {"rows_received": 0, "batches": 0}

    batches = chunk(rows, BATCH_SIZE)

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    try:
        async with pool.acquire() as conn:
            for b in batches:
                await conn.execute(UPSERT_MEAL_SQL, json.dumps(b), user_id)
    finally:
        await pool.close()

    return {"rows_received": len(rows), "batches": len(batches)}

