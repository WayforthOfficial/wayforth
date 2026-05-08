"""routers/billing.py — Billing, subscriptions, account, and Stripe webhook routes."""

import asyncio
import hashlib
import logging
import math
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import _resolve_user
from core.credits import (
    PLANS, CREDITS_PER_CALL, ROUTING_FEE,
    check_and_deduct_credits, compute_calls_remaining, _dispatch_webhooks,
)
from core.db import get_db
from core.rate_limit import limiter, get_real_ip
from core.tier_gates import require_tier
from services.managed import SERVICE_DISPLAY_NAMES

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Local STRIPE_MOCK flag (avoids circular import from main) ─────────────────

STRIPE_MOCK = (
    not os.environ.get("STRIPE_SECRET_KEY", "")
    or os.environ.get("STRIPE_MOCK", "false").lower() == "true"
    or os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_")
)

# ── Constants ─────────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free":       {"rpm": 10,  "monthly": 1_000,    "fee_bps": 150},
    "builder":    {"rpm": 30,  "monthly": 5_000,    "fee_bps": 150},
    "starter":    {"rpm": 60,  "monthly": 20_000,   "fee_bps": 150},
    "pro":        {"rpm": 120, "monthly": 100_000,  "fee_bps": 150},
    "growth":     {"rpm": 300, "monthly": 500_000,  "fee_bps": 150},
    "enterprise": {"rpm": 500, "monthly": -1,       "fee_bps": 150},
}

PACKAGES = {
    "builder":    {"credits": 6_000,   "price_usd": 12,  "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Builder"},
    "starter":    {"credits": 21_000,  "price_usd": 29,  "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Starter"},
    "pro":        {"credits": 72_000,  "price_usd": 99,  "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Pro"},
    "growth":     {"credits": 240_000, "price_usd": 299, "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Growth"},
    "enterprise": {"credits": -1,      "price_usd": None,"wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Enterprise"},
}

# Stripe packages (also defined in core.credits.STRIPE_PACKAGES — replicated here to avoid
# circular imports when billing.py needs the price_cents / price_id for checkout)
STRIPE_PACKAGES = {
    "builder": {"price_cents": 1200,  "credits": 6_000,   "label": "Builder",
                "price_id": os.environ.get("STRIPE_PRICE_BUILDER", "")},
    "starter": {"price_cents": 2900,  "credits": 21_000,  "label": "Starter",
                "price_id": os.environ.get("STRIPE_PRICE_STARTER", "")},
    "pro":     {"price_cents": 9900,  "credits": 72_000,  "label": "Pro",
                "price_id": os.environ.get("STRIPE_PRICE_PRO", "")},
    "growth":  {"price_cents": 29900, "credits": 240_000, "label": "Growth",
                "price_id": os.environ.get("STRIPE_PRICE_GROWTH", "")},
}

_TIER_FEATURES = {
    "free":     {"execute_managed": True,  "byok": False, "analytics": False, "priority_support": False},
    "builder":  {"execute_managed": True,  "byok": True,  "analytics": False, "priority_support": False},
    "starter":  {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": False},
    "pro":      {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": True},
    "growth":   {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": True},
}

# ── Models ────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    category: str
    price_per_call: float = 0.0
    contact_email: str | None = None


class PayRequest(BaseModel):
    service_id: str
    service_owner: str = ""
    amount_usd: float = 0.0
    query_id: str = ""
    agent_id: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _credits_to_tier(lifetime_credits: int, package_tier: str | None) -> str:
    if package_tier and package_tier in _TIER_FEATURES:
        return package_tier
    if lifetime_credits >= 240_000:
        return "growth"
    if lifetime_credits >= 72_000:
        return "pro"
    if lifetime_credits >= 21_000:
        return "starter"
    if lifetime_credits >= 6_000:
        return "builder"
    return "free"


def _account_auth_key(request: Request):
    """Return (raw_key, key_hash) from X-Wayforth-API-Key header, or raise 401."""
    raw = request.headers.get("X-Wayforth-API-Key", "")
    if not raw:
        raise HTTPException(status_code=401, detail="API key required")
    return raw, hashlib.sha256(raw.encode()).hexdigest()


async def _verify_tx_on_base(tx_hash: str, expected_recipient: str, expected_amount_usdc: str) -> dict:
    """Verify a USDC transfer tx_hash on Base using eth_getTransactionReceipt.

    Returns {valid, reason}. Accepts optimistically on RPC failure.
    """
    BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    if not BASE_RPC_URL:
        return {"valid": True, "reason": "no_rpc_configured"}

    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(BASE_RPC_URL, json=payload)
        if r.status_code != 200:
            return {"valid": True, "reason": "rpc_unavailable"}

        receipt = r.json().get("result")
        if not receipt:
            return {"valid": False, "reason": "transaction_not_found"}
        if receipt.get("status") != "0x1":
            return {"valid": False, "reason": "transaction_failed_on_chain"}

        # Find Transfer log from USDC contract to expected_recipient
        for log in receipt.get("logs", []):
            if log.get("address", "").lower() != USDC_ADDRESS.lower():
                continue
            topics = log.get("topics", [])
            if not topics or topics[0].lower() != TRANSFER_TOPIC.lower():
                continue
            # topics[2] is the `to` address (padded to 32 bytes)
            if len(topics) < 3:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != expected_recipient.lower():
                continue
            # Amount is in log data (USDC has 6 decimals)
            try:
                amount_micro = int(log.get("data", "0x0"), 16)
                amount_usdc = amount_micro / 1_000_000
                expected = float(expected_amount_usdc)
                if abs(amount_usdc - expected) / expected <= 0.02:
                    return {"valid": True, "reason": "confirmed", "amount_usdc": str(amount_usdc)}
                return {
                    "valid": False,
                    "reason": f"amount_mismatch: expected ${expected:.6f}, received ${amount_usdc:.6f}",
                }
            except (ValueError, ZeroDivisionError):
                continue

        return {"valid": False, "reason": "no_matching_usdc_transfer_found"}

    except httpx.TimeoutException:
        return {"valid": True, "reason": "optimistic_rpc_timeout"}
    except Exception as _e:
        logger.warning("_verify_tx_on_base error: %s", _e)
        return {"valid": True, "reason": "optimistic_rpc_error"}


# ── USDC subscription helpers ─────────────────────────────────────────────────

async def _activate_usdc_subscription(pool, reference_id: str, tx_hash: str,
                                       plan: str, credits_total: int,
                                       payer_address: str, api_key_id: str,
                                       bonus_credits: int = 0):
    """Activate a USDC subscription after payment confirmation."""
    async with pool.acquire() as db:
        async with db.transaction():
            await db.execute("""
                UPDATE usdc_payments
                SET status = 'confirmed', tx_hash = $1, confirmed_at = NOW(),
                    bonus_credits = $3
                WHERE reference_id = $2
            """, tx_hash, reference_id, bonus_credits)
            await db.execute("""
                UPDATE api_keys
                SET payment_rail = 'usdc',
                    tier = $1,
                    subscription_expires_at = NOW() + INTERVAL '30 days',
                    usdc_wallet_address = $2,
                    subscription_status = 'active'
                WHERE id = $3::uuid
            """, plan, payer_address, api_key_id)
            user_row = await db.fetchrow(
                "SELECT user_id FROM api_keys WHERE id = $1::uuid", api_key_id
            )
            if user_row:
                plan_def = PLANS.get(plan, PLANS["free"])
                await db.execute("""
                    INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, $3, 'usdc')
                    ON CONFLICT (user_id) DO UPDATE
                    SET credits_balance = user_credits.credits_balance + $2,
                        lifetime_credits = user_credits.lifetime_credits + $2,
                        package_tier = $3,
                        payment_method = 'usdc',
                        updated_at = NOW()
                """, str(user_row["user_id"]), credits_total, plan)
                asyncio.create_task(_dispatch_webhooks(
                    str(user_row["user_id"]), "subscription.activated", {
                        "plan": plan,
                        "payment_rail": "usdc",
                        "credits_added": credits_total,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ))


async def _usdc_payment_watcher():
    """Background task: poll Base chain for USDC transfers to WAYFORTH_BASE_WALLET."""
    from main import app
    BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    last_block = "latest"
    while True:
        try:
            await asyncio.sleep(30)
            wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
            if not wallet or not app.state.pool:
                continue

            async with app.state.pool.acquire() as db:
                pending = await db.fetch(
                    "SELECT id, reference_id, plan, amount_usdc, api_key_id "
                    "FROM usdc_payments WHERE status = 'pending' AND expires_at > NOW()"
                )
                # Mark expired rows
                await db.execute(
                    "UPDATE usdc_payments SET status = 'expired' "
                    "WHERE status = 'pending' AND expires_at <= NOW()"
                )

            if not pending:
                continue

            # Fetch Transfer events to our wallet
            padded_wallet = "0x" + "0" * 24 + wallet[2:].lower()
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{
                    "fromBlock": last_block if last_block != "latest" else "0x1",
                    "toBlock": "latest",
                    "address": USDC_ADDRESS,
                    "topics": [TRANSFER_TOPIC, None, padded_wallet],
                }],
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(BASE_RPC_URL, json=payload)
            if r.status_code != 200:
                continue

            logs = r.json().get("result", [])
            last_block = "latest"

            for log in logs:
                tx_hash = log.get("transactionHash", "")
                # Amount is the last 32 bytes of data field (USDC has 6 decimals)
                data = log.get("data", "0x")
                try:
                    amount_micro = int(data, 16)
                    amount_usdc = amount_micro / 1_000_000
                except ValueError:
                    continue

                for row in pending:
                    expected = float(row["amount_usdc"])
                    # Match within 1%
                    if abs(amount_usdc - expected) / expected <= 0.01:
                        plan_def = PLANS.get(row["plan"], PLANS["free"])
                        base_credits = plan_def["monthly_credits"]
                        bonus = math.floor(base_credits * 0.05)
                        await _activate_usdc_subscription(
                            app.state.pool,
                            row["reference_id"],
                            tx_hash,
                            row["plan"],
                            base_credits + bonus,
                            wallet,  # payer address from log topics[1]
                            str(row["api_key_id"]),
                            bonus,
                        )
                        break

        except Exception as _e:
            logger.error("_usdc_payment_watcher error: %s", _e)


async def _usdc_renewal_reminder():
    """Background task: fire renewal_due webhooks 7 days before USDC subscription expiry."""
    from main import app
    while True:
        try:
            await asyncio.sleep(3600)  # check hourly
            if not app.state.pool:
                continue
            async with app.state.pool.acquire() as db:
                expiring = await db.fetch("""
                    SELECT k.id, k.user_id, k.tier, k.subscription_expires_at
                    FROM api_keys k
                    WHERE k.payment_rail = 'usdc'
                    AND k.subscription_expires_at BETWEEN NOW() AND NOW() + INTERVAL '7 days'
                """)
            for key in expiring:
                plan = key["tier"]
                plan_def = PLANS.get(plan, PLANS["free"])
                bonus_calls = plan_def["usdc_bonus_credits"] // CREDITS_PER_CALL
                new_ref = f"sub_usdc_{secrets.token_hex(8)}"
                asyncio.create_task(_dispatch_webhooks(
                    str(key["user_id"]), "subscription.renewal_due", {
                        "event": "subscription.renewal_due",
                        "plan": plan,
                        "expires_at": key["subscription_expires_at"].isoformat(),
                        "renewal": {
                            "payment_address": os.environ.get("WAYFORTH_BASE_WALLET", ""),
                            "amount_usdc": f"{plan_def['price_usdc']:.6f}",
                            "new_reference_id": new_ref,
                            "bonus_calls": bonus_calls,
                        },
                    }
                ))
        except Exception as _e:
            logger.error("_usdc_renewal_reminder error: %s", _e)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/billing/subscribe-usdc")
@limiter.limit("10/minute")
async def subscribe_usdc(request: Request, db=Depends(get_db)):
    """Initiate a USDC subscription payment on Base.

    Returns payment instructions. A background watcher confirms the Transfer event
    and activates the subscription automatically.
    """
    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "USDC subscriptions coming soon. Use card billing for now.",
            "alternatives": ["GET /pricing/json"],
        })

    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    key_record = await db.fetchrow(
        "SELECT id, user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    body = await request.json()
    plan = body.get("plan", "").strip().lower()
    wallet_address = body.get("wallet_address", "").strip()

    if plan not in PLANS or plan == "free":
        paid_plans = [k for k in PLANS if k != "free"]
        raise HTTPException(status_code=400, detail={
            "error": f"Invalid plan '{plan}'. Choose from: {', '.join(paid_plans)}"
        })

    plan_def = PLANS[plan]
    reference_id = f"sub_usdc_{secrets.token_hex(8)}"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    amount_usdc = f"{plan_def['price_usdc']:.6f}"
    calls_included = plan_def["calls_included"]
    bonus_calls = plan_def["usdc_bonus_credits"] // CREDITS_PER_CALL

    await db.execute("""
        INSERT INTO usdc_payments
            (reference_id, api_key_id, plan, amount_usdc, wallet_address, expires_at)
        VALUES ($1, $2::uuid, $3, $4, $5, $6)
    """, reference_id, str(key_record["id"]), plan, float(amount_usdc),
        wallet_address or None, expires_at)

    return {
        "payment_address": wayforth_wallet,
        "amount_usdc": amount_usdc,
        "plan": plan,
        "calls_included": calls_included,
        "bonus_calls": bonus_calls,
        "reference_id": reference_id,
        "memo": "Include this in USDC transfer memo",
        "expires_at": expires_at.isoformat(),
        "instructions": (
            f"Send exactly {plan_def['price_usdc']} USDC to the address above on Base "
            f"within 24 hours. Include the reference_id as memo."
        ),
    }


@router.post("/billing/topup-usdc")
@limiter.limit("10/minute")
async def topup_usdc(request: Request, db=Depends(get_db)):
    """Agent-initiated USDC top-up. api_key + tx_hash in body — no Authorization header needed.

    Requires billing_permission = 'auto_topup' or 'full' on the API key.
    Enforces monthly_topup_limit_usd. Replay-protected via usdc_payments tx_hash index.
    """
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    tx_hash = body.get("tx_hash", "").strip()
    amount_usdc_str = body.get("amount_usdc", "").strip()

    if not api_key or not tx_hash or not amount_usdc_str:
        raise HTTPException(status_code=400, detail={
            "error": "api_key, tx_hash, and amount_usdc are required"
        })

    try:
        amount_usdc_float = float(amount_usdc_str)
        if amount_usdc_float <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail={"error": "amount_usdc must be a positive number"})

    # 1. Look up API key with billing settings
    key_record = await db.fetchrow("""
        SELECT id, user_id, tier, billing_permission,
               monthly_topup_limit_usd, topup_amount_usd,
               monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE key_hash = $1 AND active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    require_tier(key_record["tier"] or "free", "topup_usdc")

    # 2. Permission check
    billing_perm = key_record["billing_permission"] or "none"
    if billing_perm == "none":
        raise HTTPException(status_code=403, detail={
            "error": "billing_permission_denied",
            "message": (
                "This API key does not have billing permissions. "
                "Enable auto top-up in your dashboard at wayforth.io/dashboard"
            ),
        })

    # 3. Monthly reset if period has rolled over
    now_utc = datetime.now(timezone.utc)
    reset_at = key_record["monthly_topup_reset_at"]
    if reset_at and now_utc >= reset_at:
        next_reset = reset_at + timedelta(days=32)
        # Advance to start of next calendar month
        next_reset = next_reset.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        await db.execute("""
            UPDATE api_keys
            SET monthly_topup_spent_usd = 0,
                monthly_topup_reset_at = $1
            WHERE id = $2::uuid
        """, next_reset, str(key_record["id"]))
        spent_usd = 0.0
        reset_at = next_reset
    else:
        spent_usd = float(key_record["monthly_topup_spent_usd"] or 0)

    # 4. Budget check
    limit_usd = float(key_record["monthly_topup_limit_usd"] or 20)
    remaining_usd = round(limit_usd - spent_usd, 2)
    if amount_usdc_float > remaining_usd:
        raise HTTPException(status_code=403, detail={
            "error": "monthly_limit_reached",
            "message": (
                f"Monthly auto top-up limit of ${limit_usd:.2f} reached. "
                f"Resets on {reset_at.strftime('%Y-%m-%d') if reset_at else 'next month'}."
            ),
            "limit_usd": limit_usd,
            "spent_usd": round(spent_usd, 2),
            "resets_at": reset_at.isoformat() if reset_at else None,
        })

    # 5. WAYFORTH_BASE_WALLET guard
    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "Crypto payments not yet configured. Top up via dashboard instead.",
            "dashboard_url": "https://wayforth.io/billing",
        })

    # 6. Replay protection
    existing = await db.fetchval(
        "SELECT id FROM usdc_payments WHERE tx_hash = $1", tx_hash
    )
    if existing:
        raise HTTPException(status_code=409, detail={
            "error": "transaction_already_used",
            "message": "This transaction hash has already been applied to a top-up.",
        })

    # 7. Verify tx_hash on Base
    verify = await _verify_tx_on_base(tx_hash, wayforth_wallet, amount_usdc_str)
    if not verify["valid"]:
        raise HTTPException(status_code=402, detail={
            "error": "payment_verification_failed",
            "reason": verify.get("reason", "invalid_transaction"),
            "message": (
                f"Could not verify USDC transfer of ${amount_usdc_float:.6f} to "
                f"Wayforth wallet. Ensure the transaction is confirmed on Base mainnet."
            ),
        })

    # 8. Atomic credit + budget update — 5% USDC bonus
    base_credits = math.floor(amount_usdc_float * 1000)
    topup_bonus = math.floor(base_credits * 0.05)
    credits_to_add = base_credits + topup_bonus
    reference_id = f"topup_{secrets.token_hex(8)}"

    async with db.transaction():
        # Add credits to user
        new_credits = await db.fetchval("""
            UPDATE user_credits
            SET credits_balance = credits_balance + $1,
                lifetime_credits = lifetime_credits + $1,
                updated_at = NOW()
            WHERE user_id = $2::uuid
            RETURNING credits_balance
        """, credits_to_add, str(key_record["user_id"]))

        if new_credits is None:
            # First-time credit row
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                VALUES ($1::uuid, $2, $2, 'free', 'usdc')
            """, str(key_record["user_id"]), credits_to_add)
            new_credits = credits_to_add

        # Update monthly spend tracker
        await db.execute("""
            UPDATE api_keys
            SET monthly_topup_spent_usd = monthly_topup_spent_usd + $1
            WHERE id = $2::uuid
        """, amount_usdc_float, str(key_record["id"]))

        # Record payment for replay protection and audit
        await db.execute("""
            INSERT INTO usdc_payments
                (reference_id, api_key_id, plan, amount_usdc, tx_hash, status, bonus_credits, expires_at)
            VALUES ($1, $2::uuid, 'topup', $3, $4, 'confirmed', $5, NOW() + INTERVAL '10 years')
        """, reference_id, str(key_record["id"]), amount_usdc_float, tx_hash, topup_bonus)

    new_spent = round(spent_usd + amount_usdc_float, 2)
    new_remaining = round(limit_usd - new_spent, 2)

    return {
        "success": True,
        "amount_usdc": f"{amount_usdc_float:.6f}",
        "credits_added": credits_to_add,
        "bonus_credits": topup_bonus,
        "calls_added": credits_to_add // CREDITS_PER_CALL,
        "new_balance_calls": new_credits // CREDITS_PER_CALL,
        "monthly_topup_spent_usd": new_spent,
        "monthly_topup_limit_usd": limit_usd,
        "monthly_topup_remaining_usd": new_remaining,
        "resets_at": reset_at.isoformat() if reset_at else None,
        "tx_confirmed": True,
        "payment_rail": "usdc",
    }


@router.get("/billing/settings")
@limiter.limit("30/minute")
async def get_billing_settings(request: Request, db=Depends(get_db)):
    """Return current billing permission settings for the authenticated API key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT id, user_id, tier, payment_rail,
               billing_permission, topup_trigger_calls,
               topup_amount_usd, monthly_topup_limit_usd,
               monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE key_hash = $1 AND active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchval(
        "SELECT credits_balance FROM user_credits WHERE user_id = $1",
        key_record["user_id"],
    )
    balance = credits or 0
    tier = key_record["tier"] or "free"
    payment_rail = key_record["payment_rail"] or "card"
    limit = float(key_record["monthly_topup_limit_usd"] or 20)
    spent = float(key_record["monthly_topup_spent_usd"] or 0)
    reset_at = key_record["monthly_topup_reset_at"]
    usdc_active = payment_rail == "usdc"

    result = {
        "billing_permission": key_record["billing_permission"] or "none",
        "topup_trigger_calls": key_record["topup_trigger_calls"] or 100,
        "topup_amount_usd": float(key_record["topup_amount_usd"] or 5),
        "monthly_topup_limit_usd": limit,
        "monthly_topup_spent_usd": round(spent, 2),
        "monthly_topup_remaining_usd": round(limit - spent, 2),
        "monthly_topup_reset_at": reset_at.date().isoformat() if reset_at else None,
        "calls_remaining": await compute_calls_remaining(db, str(key_record["id"])),
        "plan": tier,
        "payment_rail": payment_rail,
        "usdc_bonus_rate": 0.05,
        "usdc_bonus_active": usdc_active,
    }
    if not usdc_active:
        result["usdc_bonus_message"] = "Switch to USDC and get 5% more calls every month."
    return result


@router.patch("/billing/settings")
@limiter.limit("10/minute")
async def update_billing_settings(request: Request, db=Depends(get_db)):
    """Update billing permission settings for the authenticated API key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow(
        "SELECT id, topup_amount_usd, monthly_topup_limit_usd FROM api_keys "
        "WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    updates: dict = {}

    if "billing_permission" in body:
        val = body["billing_permission"]
        if val not in ("none", "auto_topup", "full"):
            raise HTTPException(status_code=400, detail={
                "error": "billing_permission must be one of: none, auto_topup, full"
            })
        updates["billing_permission"] = val

    if "topup_trigger_calls" in body:
        val = int(body["topup_trigger_calls"])
        if not (50 <= val <= 1000):
            raise HTTPException(status_code=400, detail={
                "error": "topup_trigger_calls must be between 50 and 1000"
            })
        updates["topup_trigger_calls"] = val

    if "topup_amount_usd" in body:
        val = float(body["topup_amount_usd"])
        if not (1.0 <= val <= 100.0):
            raise HTTPException(status_code=400, detail={
                "error": "topup_amount_usd must be between 1.00 and 100.00"
            })
        updates["topup_amount_usd"] = val

    if "monthly_topup_limit_usd" in body:
        val = float(body["monthly_topup_limit_usd"])
        effective_topup = updates.get("topup_amount_usd", float(key_record["topup_amount_usd"] or 5))
        if val < effective_topup:
            raise HTTPException(status_code=400, detail={
                "error": f"monthly_topup_limit_usd ({val:.2f}) must be >= topup_amount_usd ({effective_topup:.2f})"
            })
        updates["monthly_topup_limit_usd"] = val

    if not updates:
        raise HTTPException(status_code=400, detail={"error": "No valid fields provided"})

    set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(updates)]
    set_clause = ", ".join(set_parts)
    values = list(updates.values())

    await db.execute(
        f"UPDATE api_keys SET {set_clause} WHERE id = $1::uuid",
        str(key_record["id"]), *values,
    )

    return await get_billing_settings(request, db)


@router.get("/account/billing-permissions")
@limiter.limit("30/minute")
async def get_billing_permissions(request: Request, db=Depends(get_db)):
    """Return billing permission settings for the authenticated API key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    row = await db.fetchrow("""
        SELECT billing_permission, topup_trigger_calls, topup_amount_usd,
               monthly_topup_limit_usd, monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE key_hash = $1 AND active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return {
        "billing_permission": row["billing_permission"] or "none",
        "topup_trigger_calls": row["topup_trigger_calls"] or 100,
        "topup_amount_usd": float(row["topup_amount_usd"] or 5),
        "monthly_topup_limit_usd": float(row["monthly_topup_limit_usd"] or 20),
        "monthly_topup_spent_usd": float(row["monthly_topup_spent_usd"] or 0),
        "monthly_topup_reset_at": row["monthly_topup_reset_at"].date().isoformat() if row["monthly_topup_reset_at"] else None,
    }


@router.put("/account/billing-permissions")
@limiter.limit("10/minute")
async def put_billing_permissions(request: Request, db=Depends(get_db)):
    """Update billing permission settings for the authenticated API key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow(
        "SELECT id, topup_amount_usd, monthly_topup_limit_usd FROM api_keys "
        "WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    updates: dict = {}

    if "billing_permission" in body:
        val = body["billing_permission"]
        if val not in ("none", "auto_topup", "full"):
            raise HTTPException(status_code=400, detail={
                "error": "billing_permission must be one of: none, auto_topup, full"
            })
        updates["billing_permission"] = val

    if "topup_trigger_calls" in body:
        val = int(body["topup_trigger_calls"])
        if not (50 <= val <= 1000):
            raise HTTPException(status_code=400, detail={
                "error": "topup_trigger_calls must be between 50 and 1000"
            })
        updates["topup_trigger_calls"] = val

    if "topup_amount_usd" in body:
        val = float(body["topup_amount_usd"])
        if not (5.0 <= val <= 100.0):
            raise HTTPException(status_code=400, detail={
                "error": "topup_amount_usd must be between 5.00 and 100.00"
            })
        updates["topup_amount_usd"] = val

    if "monthly_topup_limit_usd" in body:
        val = float(body["monthly_topup_limit_usd"])
        if not (5.0 <= val <= 500.0):
            raise HTTPException(status_code=400, detail={
                "error": "monthly_topup_limit_usd must be between 5.00 and 500.00"
            })
        effective_topup = updates.get("topup_amount_usd", float(key_record["topup_amount_usd"] or 5))
        if val < effective_topup:
            raise HTTPException(status_code=400, detail={
                "error": f"monthly_topup_limit_usd ({val:.2f}) must be >= topup_amount_usd ({effective_topup:.2f})"
            })
        updates["monthly_topup_limit_usd"] = val

    if not updates:
        raise HTTPException(status_code=400, detail={"error": "No valid fields provided"})

    set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(updates)]
    await db.execute(
        f"UPDATE api_keys SET {', '.join(set_parts)} WHERE id = $1::uuid",
        str(key_record["id"]), *list(updates.values()),
    )

    return await get_billing_permissions(request, db)


@router.post("/submit")
@limiter.limit("5/minute")
async def submit_service(request: Request, req: SubmitRequest):
    from main import app
    from notifications import send_submission_confirmation
    if not req.endpoint_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint_url must start with https://")
    if req.category not in ("inference", "data", "translation"):
        raise HTTPException(status_code=400, detail="category must be one of: inference, data, translation")
    if len(req.name) > 100:
        raise HTTPException(status_code=400, detail="name must be 100 characters or fewer")
    if len(req.description) > 500:
        raise HTTPException(status_code=400, detail="description must be 500 characters or fewer")
    if app.state.pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        import asyncpg
        async with app.state.pool.acquire() as conn:
            service_id = await conn.fetchval(
                """INSERT INTO services (name, description, endpoint_url, category, pricing_usdc, source, coverage_tier)
                   VALUES ($1, $2, $3, $4, $5, 'submitted', 0) RETURNING id""",
                req.name, req.description, req.endpoint_url, req.category, req.price_per_call,
            )
            await conn.execute(
                """INSERT INTO service_submissions (service_id, contact_email, ip_address)
                   VALUES ($1, $2, $3)""",
                service_id, req.contact_email, get_real_ip(request),
            )
        logger.info(f"submit name={req.name!r} category={req.category}")
        asyncio.create_task(_probe_new_service(str(service_id), req.endpoint_url))
        if req.contact_email:
            asyncio.create_task(asyncio.to_thread(
                send_submission_confirmation,
                req.contact_email, req.name, str(service_id), req.endpoint_url,
            ))
        await asyncio.sleep(3)
        async with app.state.pool.acquire() as conn2:
            service = await conn2.fetchrow("""
                SELECT coverage_tier, last_tested_at, consecutive_failures
                FROM services WHERE id = $1::uuid
            """, str(service_id))
        tier = service["coverage_tier"] if service else 0
        return {
            "status": "submitted",
            "service_id": str(service_id),
            "name": req.name,
            "initial_tier": tier,
            "message": f"Service submitted and probed. Current tier: {tier}. Tier 2 requires 90%+ uptime over 7 days.",
            "leaderboard_url": "https://wayforth.io/leaderboard",
            "tier3_url": "https://wayforth.io/tier3",
        }
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="A service with this endpoint URL already exists")
    except Exception as e:
        logger.error(f"Submit error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")


async def _probe_new_service(service_id: str, endpoint_url: str):
    from main import app
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(endpoint_url)
            new_tier = 1 if r.status_code < 500 else 0
            async with app.state.pool.acquire() as db:
                await db.execute("""
                    UPDATE services
                    SET coverage_tier=$1, last_tested_at=NOW(), consecutive_failures=0
                    WHERE id=$2::uuid
                """, new_tier, service_id)
                logger.info(f"New service {service_id} probed: tier {new_tier} (status {r.status_code})")
    except Exception as e:
        logger.warning(f"New service probe failed for {service_id}: {e}")


@router.get("/services/{service_id}")
@limiter.limit("30/minute")
async def get_service(request: Request, service_id: str):
    from main import app
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at
                FROM services WHERE id = $1
                """,
                service_id,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)


@router.get("/dashboard")
@limiter.limit("30/minute")
async def dashboard(request: Request, db=Depends(get_db)):
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    key = await db.fetchrow("""
        SELECT k.*, u.email, u.created_at as account_created,
               u.stripe_customer_id
        FROM api_keys k
        LEFT JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1
    """, key_hash)

    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    searches_this_month = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at >= $1
        AND session_id ILIKE $2
    """, month_start, f"%{key['key_prefix']}%") or 0

    recent = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '7 days'
        ORDER BY created_at DESC LIMIT 10
    """)

    _fee = round(ROUTING_FEE * 100, 4)
    LIMITS = {
        'free':       {'rpm': 10,  'monthly': 1000,   'fee_pct': _fee},
        'starter':    {'rpm': 30,  'monthly': 10000,  'fee_pct': _fee},
        'pro':        {'rpm': 100, 'monthly': 100000, 'fee_pct': _fee},
        'enterprise': {'rpm': 500, 'monthly': -1,     'fee_pct': _fee},
    }
    tier = key['tier'] or 'free'
    limits = LIMITS.get(tier, LIMITS['free'])

    return {
        "account": {
            "email": key['email'],
            "tier": tier,
            "created_at": key['account_created'].isoformat() if key['account_created'] else None,
            "stripe_customer_id": key['stripe_customer_id'],
        },
        "api_key": {
            "prefix": key['key_prefix'],
            "created_at": key['created_at'].isoformat(),
            "subscription_status": key.get('subscription_status', 'active'),
            "current_period_end": key['current_period_end'].isoformat() if key.get('current_period_end') else None,
        },
        "usage": {
            "searches_this_month": searches_this_month,
            "monthly_limit": limits['monthly'],
            "pct_used": round((searches_this_month / limits['monthly'] * 100), 1) if limits['monthly'] > 0 else 0,
            "rate_limit_rpm": limits['rpm'],
        },
        "recent_searches": [
            {"query": r['query'], "at": r['created_at'].isoformat()}
            for r in recent
        ],
        "upgrade_url": "https://wayforth.io/pricing",
    }


@router.get("/billing/balance")
@limiter.limit("30/minute")
async def get_balance(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.id, k.user_id, k.tier, k.payment_rail,
               k.quota_reset_at, k.subscription_expires_at
        FROM api_keys k
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, package_tier FROM user_credits WHERE user_id = $1",
        key_record["user_id"],
    )
    balance = credits["credits_balance"] if credits else 0
    pkg_tier = credits["package_tier"] if credits else "free"
    tier = _credits_to_tier(balance, pkg_tier)

    plan_def = PLANS.get(tier, PLANS["free"])
    resets_at = key_record.get("subscription_expires_at") or key_record.get("quota_reset_at")
    payment_rail = key_record.get("payment_rail") or "card"

    return {
        "plan": tier,
        "calls_remaining": await compute_calls_remaining(db, str(key_record["id"])),
        "calls_included": plan_def["calls_included"],
        "resets_at": resets_at.isoformat() if resets_at else None,
        "payment_rail": payment_rail,
    }


@router.get("/account/credits")
@limiter.limit("30/minute")
async def account_credits(request: Request, db=Depends(get_db)):
    """Current credit balance — canonical endpoint for dashboard and agents."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.id, k.user_id, k.tier, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1",
        key_record['user_id']
    )
    balance = credits['credits_balance'] if credits else 0
    lifetime = credits['lifetime_credits'] if credits else 0
    pkg_tier = credits['package_tier'] if credits else 'free'
    tier = _credits_to_tier(lifetime, pkg_tier)

    return {
        "plan": tier,
        "calls_remaining": await compute_calls_remaining(db, str(key_record["id"])),
        "calls_included": PLANS.get(tier, PLANS["free"])["calls_included"],
        # Dashboard-only credit detail (not shown in public docs)
        "credits_remaining": balance,
        "credits_total": lifetime,
        "tier": tier,
        "email": key_record["email"],
    }


@router.get("/account/tier")
@limiter.limit("30/minute")
async def account_tier(request: Request, db=Depends(get_db)):
    """Tier and feature flags — used by the dashboard to gate UI sections."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.user_id
        FROM api_keys k
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1",
        key_record['user_id']
    )
    balance = credits['credits_balance'] if credits else 0
    lifetime = credits['lifetime_credits'] if credits else 0
    pkg_tier = credits['package_tier'] if credits else 'free'
    tier = _credits_to_tier(lifetime, pkg_tier)

    return {
        "tier": tier,
        "credits_remaining": balance,
        "credits_total": lifetime,
        "features": _TIER_FEATURES[tier],
    }


@router.get("/account/analytics")
@limiter.limit("30/minute")
async def account_analytics(request: Request, db=Depends(get_db)):
    """Per-user analytics — Pro and Growth tiers only."""
    import re as _re
    import datetime as _datetime
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id, k.id, k.monthly_calls_count, k.monthly_calls_reset_at, k.tier "
        "FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1", user_id
    )
    tier = _credits_to_tier(credits["lifetime_credits"] or 0 if credits else 0, credits["package_tier"] if credits else None)
    require_tier(tier, "analytics")

    # ── Searches — source of truth: search_analytics table ──────────────────
    searches_month = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id=$1 AND created_at >= date_trunc('month', NOW())", user_id) or 0
    searches_today = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id=$1 AND created_at >= date_trunc('day', NOW())", user_id) or 0
    searches_7d = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id=$1 AND created_at >= NOW() - INTERVAL '7 days'", user_id) or 0
    top_query_rows = await db.fetch(
        "SELECT query, COUNT(*) as count FROM search_analytics "
        "WHERE user_id=$1 AND created_at >= date_trunc('month', NOW()) AND query IS NOT NULL "
        "GROUP BY query ORDER BY count DESC LIMIT 5", user_id)

    # ── Executions — source of truth: credit_transactions type='execution' ──
    exec_month = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id=$1 AND type='execution' "
        "AND created_at >= date_trunc('month', NOW())", user_id) or 0
    endpoint_rows = await db.fetch(
        "SELECT api_endpoint, COUNT(*) as count FROM credit_transactions "
        "WHERE user_id=$1 AND type='execution' AND created_at >= date_trunc('month', NOW()) "
        "AND api_endpoint IS NOT NULL GROUP BY api_endpoint ORDER BY count DESC", user_id)
    by_endpoint = {r["api_endpoint"].lstrip("/"): r["count"] for r in endpoint_rows}
    svc_rows = await db.fetch(
        "SELECT service_id, COUNT(*) as count FROM credit_transactions "
        "WHERE user_id=$1 AND type='execution' AND created_at >= date_trunc('month', NOW()) "
        "AND service_id IS NOT NULL GROUP BY service_id ORDER BY count DESC LIMIT 10", user_id)

    # ── Calls — source of truth: monthly_calls_count on api_keys ─────────────
    today = _datetime.date.today()
    if today.month == 12:
        reset = _datetime.date(today.year + 1, 1, 1)
    else:
        reset = _datetime.date(today.year, today.month + 1, 1)

    # WRI scores per service (from search→execute signal chain)
    wri_rows = await db.fetch("""
        SELECT
            clicked_slug AS service,
            ROUND(AVG(top_result_wri)) AS base_wri,
            COUNT(*) AS calls,
            MAX(created_at) AS last_called
        FROM search_analytics
        WHERE user_id = $1
          AND clicked_slug IS NOT NULL
          AND payment_followed = true
        GROUP BY clicked_slug
        ORDER BY calls DESC
    """, user_id)

    # Build v2 score lookup using same 3-tier matching as /admin/rank/recalculate
    def _slug_fn(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    def _norm_fn(name: str) -> str:
        return _re.sub(r'[^a-z0-9]', '', name.lower())

    v2_all = await db.fetch("SELECT name, wri_score FROM services WHERE wri_version = 'v2' AND wri_score IS NOT NULL")
    svc_slug_map = {_slug_fn(s["name"]): s for s in v2_all}
    svc_norm_map = {_norm_fn(s["name"]): s for s in v2_all}

    def _find_v2(slug: str):
        key = slug.lower().replace("-", "_")
        svc = svc_slug_map.get(key)
        if not svc:
            for k, s in svc_slug_map.items():
                if k.startswith(key + "_"):
                    svc = s
                    break
        if not svc:
            nk = _norm_fn(slug)
            svc = svc_norm_map.get(nk)
        if not svc:
            nk = _norm_fn(slug)
            for k, s in svc_norm_map.items():
                if k.startswith(nk):
                    svc = s
                    break
        return svc

    wri_score_entries = []
    for r in wri_rows:
        v2 = _find_v2(r["service"])
        wri_score_entries.append({
            "service": r["service"],
            "wri_score": round(float(v2["wri_score"]), 1) if v2 else (int(r["base_wri"]) if r["base_wri"] is not None else None),
            "ranking_version": "v2" if v2 else "v1",
            "calls": r["calls"],
            "last_called": r["last_called"].isoformat() if r["last_called"] else None,
        })

    plan_tier = credits["package_tier"] if credits else "free"
    plan_def = PLANS.get(plan_tier, PLANS["free"])
    calls_included = plan_def["calls_included"]
    calls_used = key_record["monthly_calls_count"] or 0
    calls_remaining = max(0, calls_included - calls_used)

    return {
        "searches": {
            "this_month": searches_month,
            "today": searches_today,
            "last_7_days": searches_7d,
        },
        "executions": {
            "this_month": exec_month,
            "by_endpoint": by_endpoint,
            "by_service": [{"service": r["service_id"], "count": r["count"]} for r in svc_rows],
        },
        "calls": {
            "used": calls_used,
            "included": calls_included,
            "remaining": calls_remaining,
            "resets_at": (
                key_record["monthly_calls_reset_at"].date().isoformat()
                if key_record["monthly_calls_reset_at"] else reset.isoformat()
            ),
        },
        "top_queries": [{"query": r["query"], "count": r["count"]} for r in top_query_rows],
        "wri_scores": wri_score_entries,
    }


@router.get("/account/searches")
@limiter.limit("30/minute")
async def account_searches(request: Request, db=Depends(get_db)):
    """Authenticated user's own search history — all tiers."""
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    rows = await db.fetch("""
        SELECT sa.query, sa.created_at, sa.result_count,
               s.name as top_result
        FROM search_analytics sa
        LEFT JOIN services s ON s.id = sa.top_result_id
        WHERE sa.user_id = $1
        ORDER BY sa.created_at DESC
        LIMIT 100
    """, user_id)
    total = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id = $1", user_id) or 0

    return {
        "searches": [
            {
                "query": r["query"],
                "timestamp": r["created_at"].isoformat(),
                "results_count": r["result_count"] or 0,
                "top_result": r["top_result"],
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/account/executions")
@limiter.limit("30/minute")
async def account_executions(request: Request, db=Depends(get_db)):
    """Authenticated user's own execution history — all tiers."""
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    rows = await db.fetch("""
        SELECT service_id, created_at, ABS(amount) as credits_used, type
        FROM credit_transactions
        WHERE user_id = $1 AND type IN ('execution', 'execution_refund')
        ORDER BY created_at DESC
        LIMIT 100
    """, user_id)
    total = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id=$1 AND type IN ('execution','execution_refund')", user_id) or 0

    return {
        "executions": [
            {
                "service": r["service_id"],
                "timestamp": r["created_at"].isoformat(),
                "credits_used": r["credits_used"],
                "status": "refunded" if r["type"] == "execution_refund" else "success",
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/account/agents", tags=["Account"])
@limiter.limit("30/minute")
async def account_agents(request: Request, db=Depends(get_db)):
    """Per-agent usage breakdown for the authenticated user."""
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_record = await db.fetchrow(
        "SELECT user_id, tier FROM api_keys WHERE key_hash=$1 AND active=true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    require_tier(key_record["tier"] or "free", "account_agents")
    user_id = key_record["user_id"]

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    rows = await db.fetch("""
        SELECT
            agent_id,
            COUNT(*) AS calls_total,
            COUNT(*) FILTER (WHERE created_at >= $2) AS calls_this_month,
            ABS(SUM(amount)) AS credits_used,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen,
            ARRAY_AGG(DISTINCT service_id ORDER BY service_id) FILTER (WHERE service_id IS NOT NULL) AS services
        FROM credit_transactions
        WHERE user_id = $1::uuid
          AND agent_id IS NOT NULL
          AND type IN ('execution', 'cross_rail')
        GROUP BY agent_id
        ORDER BY calls_total DESC
    """, user_id, month_start)

    untagged = await db.fetchval("""
        SELECT COUNT(*) FROM credit_transactions
        WHERE user_id = $1::uuid
          AND agent_id IS NULL
          AND type IN ('execution', 'cross_rail')
          AND created_at >= $2
    """, user_id, month_start) or 0

    agents_out = []
    for row in rows:
        total_days = max(1, (row["last_seen"] - row["first_seen"]).days + 1) if row["first_seen"] and row["last_seen"] else 1
        top_svcs = (row["services"] or [])[:3]
        agents_out.append({
            "agent_id": row["agent_id"],
            "calls_total": row["calls_total"],
            "calls_this_month": row["calls_this_month"],
            "credits_used": int(row["credits_used"] or 0),
            "top_services": top_svcs,
            "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            "avg_calls_per_day": round(row["calls_total"] / total_days, 1),
        })

    return {
        "period": "month",
        "from": month_start.isoformat(),
        "to": now.replace(hour=23, minute=59, second=59).isoformat(),
        "agents": agents_out,
        "untagged_calls": int(untagged),
        "total_agents": len(agents_out),
    }


@router.get("/account/agents/{agent_id}", tags=["Account"])
@limiter.limit("30/minute")
async def account_agent_detail(request: Request, agent_id: str, db=Depends(get_db)):
    """Detailed usage breakdown for a single agent_id."""
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_record = await db.fetchrow(
        "SELECT user_id, tier FROM api_keys WHERE key_hash=$1 AND active=true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    require_tier(key_record["tier"] or "free", "account_agents")
    user_id = key_record["user_id"]

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    summary_row = await db.fetchrow("""
        SELECT
            COUNT(*) AS calls_total,
            COUNT(*) FILTER (WHERE created_at >= $3) AS calls_this_month,
            ABS(SUM(amount)) AS credits_used_total,
            ABS(SUM(amount) FILTER (WHERE created_at >= $3)) AS credits_used_this_month,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen
        FROM credit_transactions
        WHERE user_id = $1::uuid AND agent_id = $2
          AND type IN ('execution', 'cross_rail')
    """, user_id, agent_id, month_start)

    if not summary_row or not summary_row["calls_total"]:
        raise HTTPException(status_code=404, detail={
            "error": "agent_not_found",
            "agent_id": agent_id,
            "message": "No calls recorded for this agent_id under your account.",
        })

    # Per-service breakdown
    svc_rows = await db.fetch("""
        SELECT
            service_id AS service_slug,
            COUNT(*) AS calls,
            ABS(SUM(amount)) AS credits_used,
            MAX(created_at) AS last_used
        FROM credit_transactions
        WHERE user_id = $1::uuid AND agent_id = $2
          AND type IN ('execution', 'cross_rail')
          AND service_id IS NOT NULL
        GROUP BY service_id
        ORDER BY calls DESC
        LIMIT 10
    """, user_id, agent_id)

    # Daily usage — last 30 days
    daily_rows = await db.fetch("""
        SELECT
            DATE(created_at) AS day,
            COUNT(*) AS calls,
            ABS(SUM(amount)) AS credits_used
        FROM credit_transactions
        WHERE user_id = $1::uuid AND agent_id = $2
          AND type IN ('execution', 'cross_rail')
          AND created_at >= $3
        GROUP BY DATE(created_at)
        ORDER BY day DESC
    """, user_id, agent_id, thirty_days_ago)

    return {
        "agent_id": agent_id,
        "summary": {
            "calls_total": summary_row["calls_total"],
            "calls_this_month": summary_row["calls_this_month"],
            "credits_used_total": int(summary_row["credits_used_total"] or 0),
            "credits_used_this_month": int(summary_row["credits_used_this_month"] or 0),
            "first_seen": summary_row["first_seen"].isoformat() if summary_row["first_seen"] else None,
            "last_seen": summary_row["last_seen"].isoformat() if summary_row["last_seen"] else None,
        },
        "services_breakdown": [
            {
                "service_slug": r["service_slug"],
                "service_name": SERVICE_DISPLAY_NAMES.get(r["service_slug"], r["service_slug"]),
                "calls": r["calls"],
                "credits_used": int(r["credits_used"] or 0),
                "last_used": r["last_used"].isoformat() if r["last_used"] else None,
            }
            for r in svc_rows
        ],
        "daily_usage": [
            {
                "date": str(r["day"]),
                "calls": r["calls"],
                "credits_used": int(r["credits_used"] or 0),
            }
            for r in daily_rows
        ],
    }


@router.get("/billing/packages")
async def get_packages(request: Request):
    result = []
    for key, pkg in PACKAGES.items():
        if pkg['price_usd'] is None:
            continue
        result.append({
            "id": key,
            "label": pkg['label'],
            "credits": pkg['credits'],
            "price_usd": pkg['price_usd'],
            "price_per_credit": round(pkg['price_usd'] / pkg['credits'], 8),
        })
    return {"packages": result}


@router.get("/billing/transactions")
@limiter.limit("20/minute")
async def get_transactions(request: Request, limit: int = 50, offset: int = 0, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    txs = await db.fetch("""
        SELECT id, amount, balance_after, type, description,
               api_endpoint, service_id, created_at
        FROM credit_transactions
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """, key_record['user_id'], limit, offset)

    total = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id = $1",
        key_record['user_id']
    )

    _type_map = {
        "usage": "execution", "byok": "execution", "managed": "execution",
        "byok_10pct": "execution", "managed_30pct": "execution",
        "purchase": "purchase", "mock_purchase": "purchase",
        "mock_topup": "credits_added", "refund": "refund",
    }

    def _clean_tx(t):
        row = dict(t)
        raw_type = row.get("type", "")
        row["type"] = _type_map.get(raw_type, raw_type)
        desc = row.get("description", "") or ""
        desc = desc.replace("API call: /call/", "Execution: ").replace("API call: /billing/deduct", "Service payment")
        if row["type"] == "credits_added" and "mock" in desc.lower():
            desc = "Credits added (test)"
        row["description"] = desc
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
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    purchases = await db.fetch("""
        SELECT id, package_name, credits_total, payment_method,
               payment_status, amount_usd, tx_hash, purchased_at
        FROM package_purchases
        WHERE user_id = $1
        ORDER BY purchased_at DESC
    """, key_record['user_id'])

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


@router.post("/billing/checkout")
@limiter.limit("10/minute")
async def create_checkout(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow("""
        SELECT k.user_id, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    package = body.get("package", "starter")

    if package not in STRIPE_PACKAGES:
        raise HTTPException(status_code=400, detail="Invalid package")

    pkg = STRIPE_PACKAGES[package]

    # Mock mode: no real Stripe key configured or STRIPE_MOCK=true
    if STRIPE_MOCK:
        mock_session_id = "mock_sess_" + secrets.token_hex(12)
        async with db.transaction():
            existing = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id=$1::uuid FOR UPDATE",
                key_record['user_id']
            )
            if existing:
                new_balance = existing['credits_balance'] + pkg["credits"]
                await db.execute("""
                    UPDATE user_credits
                    SET credits_balance=$1, lifetime_credits=lifetime_credits+$2,
                        package_tier=$3, payment_method='mock_card', updated_at=NOW()
                    WHERE user_id=$4::uuid
                """, new_balance, pkg["credits"], package, key_record['user_id'])
            else:
                new_balance = pkg["credits"]
                await db.execute("""
                    INSERT INTO user_credits
                    (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, $3, 'mock_card')
                """, key_record['user_id'], new_balance, package)

            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description)
                VALUES ($1::uuid, $2, $3, 'mock_purchase', $4)
            """, key_record['user_id'], pkg["credits"], new_balance,
                f"Mock purchase: {package} pack - {pkg['credits']:,} credits (Stripe not configured)")

        return {
            "checkout_url": f"https://wayforth.io/dashboard?purchase=success&package={package}&mock=true",
            "session_id": mock_session_id,
            "package": package,
            "credits": pkg["credits"],
            "price_usd": pkg["price_cents"] / 100,
            "mock": True,
            "credits_added": pkg["credits"],
            "new_balance": new_balance,
            "note": "Stripe not configured. Credits added automatically in mock mode.",
        }

    use_subscription = bool(pkg.get("price_id")) and not STRIPE_MOCK

    try:
        if use_subscription:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{"price": pkg["price_id"], "quantity": 1}],
                mode="subscription",
                success_url="https://wayforth.io/dashboard?purchase=success&package=" + package,
                cancel_url="https://wayforth.io/dashboard?purchase=cancelled",
                customer_email=key_record['email'],
                subscription_data={
                    "metadata": {
                        "user_id": str(key_record['user_id']),
                        "package": package,
                        "credits": str(pkg["credits"]),
                    }
                },
                metadata={
                    "user_id": str(key_record['user_id']),
                    "package": package,
                    "credits": str(pkg["credits"]),
                },
            )
        else:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": pkg["price_cents"],
                        "product_data": {
                            "name": f"Wayforth {pkg['label']}",
                            "description": f"{pkg['credits']:,} credits · 1 credit = $0.001",
                        },
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url="https://wayforth.io/dashboard?purchase=success&package=" + package,
                cancel_url="https://wayforth.io/dashboard?purchase=cancelled",
                customer_email=key_record['email'],
                metadata={
                    "user_id": str(key_record['user_id']),
                    "package": package,
                    "credits": str(pkg["credits"]),
                },
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    await db.execute("""
        INSERT INTO package_purchases
        (user_id, package_name, credits_purchased, credits_total,
         amount_usd, payment_method, payment_status, stripe_payment_id)
        VALUES ($1, $2, $3, $3, $4, 'card', 'pending', $5)
    """, key_record['user_id'], package, pkg['credits'],
        pkg['price_cents'] / 100, session.id)

    return {
        "checkout_url": session.url,
        "session_id": session.id,
        "package": package,
        "credits": pkg["credits"],
        "price_usd": pkg["price_cents"] / 100,
        "billing_mode": "subscription" if use_subscription else "one_time",
    }


@router.post("/billing/cancel")
@limiter.limit("5/minute")
async def billing_cancel(request: Request, db=Depends(get_db)):
    """Cancel the caller's Stripe subscription at period end."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.user_id, k.stripe_subscription_id, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    sub_id = key_record["stripe_subscription_id"]

    if not sub_id or STRIPE_MOCK:
        return JSONResponse(content={
            "message": "Contact support@wayforth.io to cancel your subscription"
        })

    try:
        sub = stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    period_end = datetime.fromtimestamp(sub["current_period_end"], tz=timezone.utc)
    effective_date = period_end.strftime("%Y-%m-%d")
    human_date = period_end.strftime("%B %-d, %Y")

    return {
        "cancelled": True,
        "effective_date": effective_date,
        "message": f"Your plan will downgrade to Free on {human_date}",
    }


@router.post("/billing/mock-topup")
@limiter.limit("5/minute")
async def mock_topup(request: Request, db=Depends(get_db)):
    """Test endpoint: add credits without Stripe. Only works when STRIPE_MOCK=true or no Stripe key set."""
    if not STRIPE_MOCK:
        raise HTTPException(status_code=403, detail="Mock top-up not available in production")

    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash=$1 AND active=true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    credits = min(int(body.get("credits", 10000)), 100000)

    async with db.transaction():
        existing = await db.fetchrow(
            "SELECT credits_balance FROM user_credits WHERE user_id=$1::uuid FOR UPDATE",
            key_record['user_id']
        )
        if existing:
            new_balance = existing['credits_balance'] + credits
            await db.execute(
                "UPDATE user_credits SET credits_balance=$1, lifetime_credits=lifetime_credits+$2, updated_at=NOW() WHERE user_id=$3::uuid",
                new_balance, credits, key_record['user_id']
            )
        else:
            new_balance = credits
            await db.execute(
                "INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method) VALUES ($1::uuid, $2, $2, 'mock', 'mock')",
                key_record['user_id'], new_balance
            )

        await db.execute("""
            INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $3, 'mock_topup', 'Mock top-up for testing')
        """, key_record['user_id'], credits, new_balance)

    return {"status": "ok", "credits_added": credits, "new_balance": new_balance, "mock": True}


@router.post("/stripe/webhook")
@limiter.limit("100/minute")
async def stripe_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        raise HTTPException(status_code=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = meta.get("user_id")
        package = meta.get("package")
        credits = int(meta.get("credits", 0))
        session_id = session.get("id")

        if not all([user_id, package, credits]):
            return {"status": "missing_metadata"}

        already = await db.fetchval(
            "SELECT id FROM package_purchases WHERE stripe_payment_id = $1 AND payment_status = 'completed'",
            session_id
        )
        if already:
            return {"status": "already_processed"}

        async with db.transaction():
            await db.execute(
                "UPDATE package_purchases SET payment_status = 'completed' WHERE stripe_payment_id = $1",
                session_id
            )

            existing = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                user_id
            )

            if existing:
                new_balance = existing['credits_balance'] + credits
                await db.execute("""
                    UPDATE user_credits
                    SET credits_balance = $1, lifetime_credits = lifetime_credits + $2,
                        package_tier = $3, payment_method = 'card', updated_at = NOW()
                    WHERE user_id = $4::uuid
                """, new_balance, credits, package, user_id)
            else:
                new_balance = credits
                await db.execute("""
                    INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, $3, 'card')
                """, user_id, credits, package)

            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description)
                VALUES ($1::uuid, $2, $3, 'purchase', $4)
            """, user_id, credits, new_balance,
                f"Stripe purchase: {package} pack — {credits:,} credits added")

        # Store subscription_id on the api_key if this was a subscription checkout
        sub_id = session.get("subscription")
        if sub_id:
            await db.execute("""
                UPDATE api_keys
                SET stripe_subscription_id = $1, subscription_status = 'active'
                WHERE user_id = $2::uuid
            """, sub_id, user_id)

        return {"status": "credited", "credits_added": credits, "new_balance": new_balance}

    elif event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if not sub_id:
            return {"status": "no_subscription"}

        # Check if this is a provider subscription first
        provider_row = await db.fetchrow(
            "SELECT id FROM providers WHERE stripe_subscription_id = $1", sub_id
        )
        if not provider_row:
            try:
                sub_obj = stripe.Subscription.retrieve(sub_id)
                provider_id_meta = sub_obj.get("metadata", {}).get("provider_id")
                provider_tier_meta = sub_obj.get("metadata", {}).get("provider_tier")
                if provider_id_meta and provider_tier_meta:
                    await db.execute(
                        "UPDATE providers SET tier = $1, stripe_subscription_id = $2 WHERE id = $3::uuid",
                        provider_tier_meta, sub_id, provider_id_meta,
                    )
                    return {"status": "provider_upgraded", "tier": provider_tier_meta}
            except Exception:
                pass

        key_row = await db.fetchrow(
            "SELECT user_id FROM api_keys WHERE stripe_subscription_id = $1",
            sub_id
        )
        if not key_row:
            return {"status": "unknown_subscription"}
        user_id = str(key_row["user_id"])

        # Determine package from subscription metadata
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            meta = sub.get("metadata", {})
            package = meta.get("package", "")
            credits = int(meta.get("credits", 0))
        except Exception:
            package, credits = "", 0

        if not credits:
            # Infer from amount
            amount_paid = invoice.get("amount_paid", 0)
            for pkg_name, pkg_data in STRIPE_PACKAGES.items():
                if pkg_data["price_cents"] == amount_paid:
                    package = pkg_name
                    credits = pkg_data["credits"]
                    break

        if credits:
            async with db.transaction():
                existing = await db.fetchrow(
                    "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                    user_id
                )
                if existing:
                    new_balance = existing["credits_balance"] + credits
                    await db.execute("""
                        UPDATE user_credits
                        SET credits_balance = $1, lifetime_credits = lifetime_credits + $2,
                            package_tier = $3, payment_method = 'card', updated_at = NOW()
                        WHERE user_id = $4::uuid
                    """, new_balance, credits, package, user_id)
                else:
                    new_balance = credits
                    await db.execute("""
                        INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                        VALUES ($1::uuid, $2, $2, $3, 'card')
                    """, user_id, credits, package)
                await db.execute("""
                    INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
                    VALUES ($1::uuid, $2, $3, 'subscription_renewal', $4)
                """, user_id, credits, new_balance,
                    f"Monthly renewal: {package} — {credits:,} credits")

            await db.execute(
                "UPDATE api_keys SET subscription_status = 'active' WHERE stripe_subscription_id = $1",
                sub_id
            )
        return {"status": "renewed", "credits_added": credits}

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        if not sub_id:
            return {"status": "no_id"}
        await db.execute("""
            UPDATE api_keys
            SET stripe_subscription_id = NULL, subscription_status = 'cancelled'
            WHERE stripe_subscription_id = $1
        """, sub_id)
        return {"status": "subscription_cancelled"}

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if not sub_id:
            return {"status": "no_subscription"}
        # Mark grace period — subscription still active but payment failed
        await db.execute("""
            UPDATE api_keys
            SET subscription_status = 'past_due'
            WHERE stripe_subscription_id = $1
        """, sub_id)
        return {"status": "payment_failed_grace_period"}

    return {"status": "ignored"}


@router.get("/system/health")
async def system_health(request: Request, db=Depends(get_db)):
    """Comprehensive health check for all payment tracks and subsystems."""
    import time as _time
    from main import app, VERSION
    from core.auth import get_fernet
    start = _time.time()

    health = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "subsystems": {},
    }

    # Database
    try:
        await db.fetchval("SELECT 1")
        health["subsystems"]["database"] = {"status": "ok"}
    except Exception as e:
        health["subsystems"]["database"] = {"status": "error", "detail": str(e)[:100]}
        health["status"] = "degraded"

    # Credits system
    try:
        count = await db.fetchval("SELECT COUNT(*) FROM user_credits")
        health["subsystems"]["credits"] = {"status": "ok", "accounts": count}
    except Exception as e:
        health["subsystems"]["credits"] = {"status": "error", "detail": str(e)[:100]}

    # BYOK
    try:
        key_count = await db.fetchval("SELECT COUNT(*) FROM user_service_keys WHERE active=true")
        health["subsystems"]["byok"] = {"status": "ok", "active_keys": key_count}
    except Exception as e:
        health["subsystems"]["byok"] = {"status": "error", "detail": str(e)[:100]}

    # Stripe
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    health["subsystems"]["stripe"] = {
        "status": "mock" if STRIPE_MOCK else "configured",
        "mode": "test" if stripe_key.startswith("sk_test_") else ("live" if stripe_key.startswith("sk_live_") else "not_set"),
    }

    # Payment tracks
    health["subsystems"]["payment_tracks"] = {
        "track_a_card": {
            "status": "mock" if STRIPE_MOCK else "active",
            "processor": "Stripe Treasury",
            "credits_deduction": "active",
        },
        "track_b_crypto": {
            "status": "active",
            "network": "base-sepolia",
            "calldata_generation": "active",
            "escrow": "0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809",
        },
        "track_c_x402": {
            "status": "active" if (os.environ.get("CDP_API_KEY_NAME") and os.environ.get("CDP_API_KEY_PRIVATE_KEY")) else "fallback_to_card",
            "auto_detection": "active",
            "facilitator": "Coinbase CDP",
            "settlement": "live" if (os.environ.get("CDP_API_KEY_NAME") and os.environ.get("CDP_API_KEY_PRIVATE_KEY")) else "not_configured",
        },
    }

    # Services catalog
    try:
        total = await db.fetchval("SELECT COUNT(*) FROM services")
        tier2 = await db.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2")
        x402 = await db.fetchval("SELECT COUNT(*) FROM services WHERE x402_supported=true")
        health["subsystems"]["catalog"] = {
            "status": "ok",
            "total_services": total,
            "tier2_verified": tier2,
            "x402_native": x402,
        }
    except Exception as e:
        health["subsystems"]["catalog"] = {"status": "error", "detail": str(e)[:100]}

    # Encryption
    try:
        enc_key = os.environ.get("ENCRYPTION_KEY", "")
        if enc_key:
            f = get_fernet()
            token = f.encrypt(b"test").decode()
            f.decrypt(token.encode())
            health["subsystems"]["encryption"] = {"status": "ok", "algorithm": "Fernet-AES128"}
        else:
            health["subsystems"]["encryption"] = {
                "status": "not_configured",
                "note": "ENCRYPTION_KEY not set — BYOK key encryption unavailable",
            }
    except Exception as e:
        health["subsystems"]["encryption"] = {"status": "error", "detail": str(e)[:100]}

    # Managed services
    managed_key_vars = {
        "groq": "GROQ_API_KEY", "deepl": "DEEPL_API_KEY",
        "openweather": "OPENWEATHER_API_KEY", "newsapi": "NEWSAPI_API_KEY",
        "resend": "RESEND_API_KEY", "serper": "SERPER_API_KEY",
        "assemblyai": "ASSEMBLYAI_API_KEY", "stability": "STABILITY_API_KEY",
        "tavily": "TAVILY_API_KEY", "jina": "JINA_API_KEY",
        "alphavantage": "ALPHA_VANTAGE_API_KEY",
    }
    configured = [s for s, v in managed_key_vars.items() if os.environ.get(v)]
    missing = [s for s, v in managed_key_vars.items() if not os.environ.get(v)]
    health["subsystems"]["managed_services"] = {
        "status": "ok" if configured else "degraded",
        "configured": configured,
        "missing": missing,
    }

    # x402 / Coinbase CDP
    cdp_key_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_private_key = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    cdp_configured = bool(cdp_key_name and cdp_private_key)
    try:
        x402_count = await db.fetchval("SELECT COUNT(*) FROM services WHERE x402_supported=true")
    except Exception:
        x402_count = 0
    health["subsystems"]["x402"] = {
        "status": "configured" if cdp_configured else "not_configured",
        "facilitator": "Coinbase CDP",
        "cdp_credentials": "set" if cdp_configured else "missing",
        "services_supported": x402_count,
    }

    health["latency_ms"] = round((_time.time() - start) * 1000)
    return health
