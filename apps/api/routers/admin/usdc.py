"""routers/admin/usdc.py — admin USDC reconciliation for stranded funds.

POST /admin/usdc/reconcile lets an operator manually credit a verified on-chain
USDC payment that the automatic watcher could not bind (e.g. the user paid from a
wallet other than the one they declared). It verifies the transfer ON-CHAIN
(recipient + amount) but, unlike the user paths, does NOT require the payer to
match a pre-declared address — the operator has manually reviewed the case. It is
gated on X-Admin-Key and must never be reachable without it.
"""

import logging
import os
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from core.db import get_db

logger = logging.getLogger("wayforth")

router = APIRouter()


@router.post("/admin/usdc/reconcile", tags=["Admin"])
async def admin_usdc_reconcile(request: Request, db=Depends(get_db)):
    """Manually credit a verified-on-chain USDC payment (stranded-funds edge case)."""
    # FINDING-110: this endpoint INTENTIONALLY bypasses the WAYFORTH_USDC_ENABLED
    # kill-switch so an operator can recover stranded funds even while the USDC
    # rail is disabled. It is admin-key gated (constant-time compare below) and
    # writes an audit log line on every successful use (see end of handler).
    from core.admin_auth import admin_authed
    if not await admin_authed(request, db):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json()
    tx_hash = (body.get("tx_hash") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    amount_usdc_str = (body.get("amount_usdc") or "").strip()
    notes = (body.get("notes") or "").strip()

    if not tx_hash or not api_key or not amount_usdc_str:
        return JSONResponse(
            {"error": "tx_hash, api_key, and amount_usdc are required"}, status_code=400,
        )
    try:
        amount_usdc_float = float(amount_usdc_str)
        if amount_usdc_float <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        return JSONResponse({"error": "amount_usdc must be a positive number"}, status_code=400)

    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        return JSONResponse({"error": "WAYFORTH_BASE_WALLET not configured"}, status_code=503)

    import hashlib
    key_record = await db.fetchrow(
        "SELECT id, user_id FROM api_keys WHERE key_hash = $1",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        return JSONResponse({"error": "invalid_api_key"}, status_code=404)

    # Replay protection: a tx hash already recorded cannot be reconciled again.
    existing = await db.fetchval("SELECT id FROM usdc_payments WHERE tx_hash = $1", tx_hash)
    if existing:
        return JSONResponse(
            {"error": "transaction_already_used",
             "message": "This tx hash has already been applied."},
            status_code=409,
        )

    # On-chain verification WITHOUT payer binding (operator has reviewed).
    from routers.billing.usdc import _verify_tx_on_base
    verify = await _verify_tx_on_base(tx_hash, wayforth_wallet, amount_usdc_str)
    if not verify.get("valid"):
        return JSONResponse(
            {"error": "payment_verification_failed", "reason": verify.get("reason")},
            status_code=402,
        )

    import math
    base_credits = math.floor(amount_usdc_float * 1000)
    bonus = math.floor(base_credits * 0.05)
    credits_to_add = base_credits + bonus
    reference_id = f"reconcile_{secrets.token_hex(8)}"

    async with db.transaction():
        # FINDING-108: claim the tx_hash FIRST via ON CONFLICT so a concurrent
        # reconcile of the same tx fails closed (clean 409, no double-credit)
        # instead of a UniqueViolationError → 500.
        claimed = await db.fetchval("""
            INSERT INTO usdc_payments
                (reference_id, api_key_id, plan, amount_usdc, tx_hash, payer_address,
                 status, consumed, bonus_credits, reconciliation_note, reconciled_by,
                 reconciled_at, expires_at)
            VALUES ($1, $2::uuid, 'reconcile', $3, $4, $5, 'confirmed', TRUE, $6, $7,
                    'admin', NOW(), NOW() + INTERVAL '10 years')
            ON CONFLICT (tx_hash) DO NOTHING
            RETURNING id
        """, reference_id, str(key_record["id"]), amount_usdc_float, tx_hash,
            verify.get("from_address"), bonus, notes)
        if claimed is None:
            return JSONResponse(
                {"error": "transaction_already_used",
                 "message": "This tx hash has already been applied."},
                status_code=409,
            )
        new_balance = await db.fetchval("""
            UPDATE user_credits
               SET credits_balance = credits_balance + $1,
                   lifetime_credits = lifetime_credits + $1,
                   updated_at = NOW()
             WHERE user_id = $2::uuid
            RETURNING credits_balance
        """, credits_to_add, str(key_record["user_id"]))
        if new_balance is None:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                VALUES ($1::uuid, $2, $2, 'free', 'usdc')
            """, str(key_record["user_id"]), credits_to_add)
            new_balance = credits_to_add
        await db.execute("""
            INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $3, 'usdc_reconcile', $4)
        """, str(key_record["user_id"]), credits_to_add, new_balance,
            f"Admin USDC reconciliation: {notes[:200]}" if notes else "Admin USDC reconciliation")

    logger.info("admin_usdc_reconcile tx=%s user=%s credits=%d by=admin",
                tx_hash[:14], str(key_record["user_id"]), credits_to_add)
    return {
        "status": "reconciled",
        "credits_added": credits_to_add,
        "bonus_credits": bonus,
        "new_balance": new_balance,
        "tx_hash": tx_hash,
        "on_chain_sender": verify.get("from_address"),
    }
