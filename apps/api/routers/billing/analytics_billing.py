"""routers/billing/analytics_billing.py — /billing/transactions, /billing/purchases, /billing/deduct."""

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core.credits import check_and_deduct_credits, PAYMENT_MULTIPLIERS
from core.db import get_db
from core.rate_limit import limiter

logger = logging.getLogger("wayforth")

router = APIRouter()


@router.get("/billing/transactions")
@limiter.limit("20/minute")
async def get_transactions(request: Request, limit: int = 50, offset: int = 0, db=Depends(get_db)):
    # Session-OR-key auth (resolve_dashboard_caller, PR #25 pattern). The dashboard's
    # billing-history page authenticates by wf_session cookie, NOT an API key. This
    # endpoint was key-only, so the cookie-authed dashboard 401'd here and rendered
    # "No transactions yet" even though credit_transactions has rows — the exact gap
    # resolve_dashboard_caller exists to close for every dashboard /billing endpoint.
    from core.auth import resolve_dashboard_caller
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    txs = await db.fetch("""
        SELECT id, amount, balance_after, type, description,
               api_endpoint, service_id, run_id, created_at
        FROM credit_transactions
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """, user_id, limit, offset)

    total = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id = $1::uuid",
        user_id
    )

    credits_row = await db.fetchrow(
        "SELECT payment_method FROM user_credits WHERE user_id = $1::uuid",
        user_id
    )
    user_payment_method = (credits_row["payment_method"] if credits_row else None) or "card"
    user_multiplier = PAYMENT_MULTIPLIERS.get(user_payment_method, 1.00)

    _type_map = {
        "usage": "execution", "byok": "execution", "managed": "execution",
        "byok_10pct": "execution", "managed_30pct": "execution",
        "purchase": "purchase", "mock_purchase": "purchase",
        "mock_topup": "credits_added", "refund": "refund",
    }
    _purchase_types = {"purchase", "credits_added"}

    def _clean_tx(t):
        row = dict(t)
        raw_type = row.get("type", "")
        row["type"] = _type_map.get(raw_type, raw_type)
        desc = row.get("description", "") or ""
        desc = desc.replace("API call: /call/", "Execution: ").replace("API call: /billing/deduct", "Service payment")
        if row["type"] == "credits_added" and "mock" in desc.lower():
            desc = "Credits added (test)"
        row["description"] = desc
        if row["type"] in _purchase_types:
            row["payment_method"] = user_payment_method
            row["multiplier_applied"] = user_multiplier
        else:
            row["payment_method"] = None
            row["multiplier_applied"] = None
        return row

    return {
        "transactions": [_clean_tx(t) for t in txs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/billing/purchases")
@limiter.limit("20/minute")
async def get_purchases(request: Request, db=Depends(get_db)):
    # Session-OR-key auth — same dashboard-cookie gap as /billing/transactions.
    from core.auth import resolve_dashboard_caller
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    purchases = await db.fetch("""
        SELECT id, package_name, credits_total, payment_method,
               payment_status, amount_usd, tx_hash, purchased_at
        FROM package_purchases
        WHERE user_id = $1::uuid
        ORDER BY purchased_at DESC
    """, user_id)

    return {"purchases": [dict(p) for p in purchases]}


@router.post("/billing/deduct")
@limiter.limit("60/minute")
async def deduct_credits(request: Request, db=Depends(get_db)):
    """Deduct credits for a service payment. Called by wayforth_pay() MCP tool."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    service_id = body.get("service_id", "unknown")
    amount_usd = float(body.get("amount_usd", 0.001))
    credits_needed = max(1, round(amount_usd * 1000))

    success, balance_after = await check_and_deduct_credits(
        db,
        str(key_record['user_id']),
        credits_needed,
        "/billing/deduct",
        service_id
    )

    if not success:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "balance": balance_after,
                "required": credits_needed,
                "top_up_url": "https://wayforth.io/dashboard",
            }
        )

    return {
        "status": "ok",
        "credits_deducted": credits_needed,
        "credits_remaining": balance_after,
        "amount_usd": amount_usd,
        "service_id": service_id,
    }
