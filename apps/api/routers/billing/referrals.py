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


async def _redeem_in_tx(conn, user_id: str, code: str) -> None:
    """BILLING-1: atomic referral redemption. Must run inside a transaction.

    The previous flow did SELECT-then-UPDATE with no row lock and no DB
    uniqueness, so two concurrent redeems by one account both passed the
    "already redeemed" check and both granted 500 credits — an unbounded farm.
    Now:
      * the redemption is claimed with a single conditional UPDATE
        (... WHERE code = $code AND referred_user_id IS NULL RETURNING id);
        a lost race returns no row → 422.
      * a partial UNIQUE index on referred_user_id (migration 062) blocks the
        same user claiming two different codes concurrently → UniqueViolationError
        mapped to 422.
      * the 500-credit grant runs ONLY after a successful claim, in the same
        transaction, so it can never be double-applied.
    """
    import asyncpg

    referral = await conn.fetchrow(
        "SELECT referrer_user_id FROM referrals WHERE code = $1", code
    )
    if not referral:
        raise HTTPException(status_code=404, detail="invalid_code")
    if str(referral["referrer_user_id"]) == user_id:
        raise HTTPException(status_code=400, detail="cannot_redeem_own_code")

    already = await conn.fetchrow(
        "SELECT 1 FROM referrals WHERE referred_user_id = $1::uuid", user_id
    )
    if already:
        raise HTTPException(status_code=422, detail="already_redeemed")

    try:
        claimed = await conn.fetchrow(
            "UPDATE referrals SET referred_user_id = $1::uuid, redeemed_at = NOW() "
            "WHERE code = $2 AND referred_user_id IS NULL RETURNING id",
            user_id, code,
        )
    except asyncpg.UniqueViolationError:
        # Concurrent redeem by the same user on a different code lost the race.
        raise HTTPException(status_code=422, detail="already_redeemed")
    if not claimed:
        raise HTTPException(status_code=422, detail="already_redeemed")

    # Grant 500 bonus monthly calls — only reached on a successful, unique claim.
    await conn.execute("""
        UPDATE api_keys
        SET monthly_calls_count = GREATEST(0, monthly_calls_count - 500)
        WHERE user_id = $1::uuid AND active = true
    """, user_id)


@router.post("/account/referral/redeem")
@limiter.limit("10/minute")
async def redeem_referral(body: ReferralRedeemRequest, request: Request, db=Depends(get_db)):
    caller = await resolve_dashboard_caller(request, db)
    async with db.transaction():
        await _redeem_in_tx(db, caller["user_id"], body.code)
    return {"redeemed": True, "bonus_calls": 500}
