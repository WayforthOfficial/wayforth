"""routers/billing/referrals.py — GET /account/referral, POST /account/referral/redeem.

Both endpoints accept tri-mode dashboard auth (wf_session cookie, Bearer JWT,
or X-Wayforth-API-Key)."""

import random
import string
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import resolve_dashboard_caller
from core.db import get_db
from core.rate_limit import limiter

router = APIRouter()


class ReferralRedeemRequest(BaseModel):
    code: str


def _new_code() -> str:
    return "WF-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


@router.get("/account/referral")
@limiter.limit("20/minute")
async def get_referral(request: Request, db=Depends(get_db)):
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    row = await db.fetchrow(
        "SELECT code FROM referrals WHERE referrer_user_id = $1::uuid LIMIT 1", user_id
    )
    if row:
        code = row["code"]
    else:
        for _ in range(10):
            code = _new_code()
            clash = await db.fetchrow("SELECT 1 FROM referrals WHERE code = $1", code)
            if not clash:
                break
        await db.execute(
            "INSERT INTO referrals (id, referrer_user_id, code, created_at) VALUES ($1, $2::uuid, $3, NOW())",
            uuid.uuid4(), user_id, code,
        )

    referrals_count = await db.fetchval("""
        SELECT COUNT(*) FROM referrals
        WHERE referrer_user_id = $1::uuid
          AND referred_user_id IS NOT NULL
          AND redeemed_at IS NOT NULL
    """, user_id) or 0

    return {
        "referral_code": code,
        "referral_url": f"https://wayforth.io?ref={code}",
        "referrals_count": int(referrals_count),
        "bonus_calls_earned": int(referrals_count) * 1_000,
    }


@router.post("/account/referral/redeem")
@limiter.limit("10/minute")
async def redeem_referral(body: ReferralRedeemRequest, request: Request, db=Depends(get_db)):
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    referral = await db.fetchrow("SELECT * FROM referrals WHERE code = $1", body.code)
    if not referral:
        raise HTTPException(status_code=404, detail="invalid_code")

    if str(referral["referrer_user_id"]) == user_id:
        raise HTTPException(status_code=400, detail="cannot_redeem_own_code")

    already = await db.fetchrow(
        "SELECT 1 FROM referrals WHERE referred_user_id = $1::uuid", user_id
    )
    if already:
        raise HTTPException(status_code=422, detail="already_redeemed")

    await db.execute(
        "UPDATE referrals SET referred_user_id = $1::uuid, redeemed_at = NOW() WHERE code = $2",
        user_id, body.code,
    )
    # Give referred user 500 bonus monthly calls (via monthly_calls_count credit)
    await db.execute("""
        UPDATE api_keys
        SET monthly_calls_count = GREATEST(0, monthly_calls_count - 500)
        WHERE user_id = $1::uuid AND active = true
    """, user_id)
    return {"redeemed": True, "bonus_calls": 500}
