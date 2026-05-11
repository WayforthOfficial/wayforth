import asyncio
import hashlib
import logging
import math
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("wayforth")

ROUTING_FEE = 0.015  # 1.5% flat, all tiers

# Blended average for DISPLAY purposes only — never used for billing logic
CREDITS_PER_CALL = 6

PLANS = {
    "free": {
        "monthly_credits":    600,
        "calls_included":     100,
        "price_usd":          0,
        "price_usdc":         0,
        "usdc_bonus_credits": 0,
        "stripe_price_env":   None,
        "features":           ["search", "execute", "wayforthrank"],
    },
    "builder": {
        "monthly_credits":    6_000,
        "calls_included":     1_000,
        "price_usd":          12,
        "price_usdc":         12,
        "usdc_bonus_credits": 300,
        "stripe_price_env":   "STRIPE_PRICE_BUILDER",
        "features":           ["search", "execute", "wayforthrank", "byok", "webhooks"],
    },
    "starter": {
        "monthly_credits":    21_000,
        "calls_included":     3_500,
        "price_usd":          29,
        "price_usdc":         29,
        "usdc_bonus_credits": 1_050,
        "stripe_price_env":   "STRIPE_PRICE_STARTER",
        "features":           ["builder_features", "analytics", "wayforthql"],
    },
    "pro": {
        "monthly_credits":    72_000,
        "calls_included":     12_000,
        "price_usd":          99,
        "price_usdc":         99,
        "usdc_bonus_credits": 3_600,
        "stripe_price_env":   "STRIPE_PRICE_PRO",
        "features":           ["starter_features", "wri_scores", "priority"],
    },
    "growth": {
        "monthly_credits":    240_000,
        "calls_included":     40_000,
        "price_usd":          299,
        "price_usdc":         299,
        "usdc_bonus_credits": 12_000,
        "stripe_price_env":   "STRIPE_PRICE_GROWTH",
        "features":           ["pro_features", "custom_services", "no_limits"],
    },
}

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

CREDIT_COSTS = {
    "search": 1,
    "query": 2,
    "intelligence": 5,
    "graph": 2,
    "wri_history": 1,
    "payment_routing": 100,  # per $1 routed
}


async def check_and_deduct_credits(db, user_id: str, cost: int, endpoint: str,
                                   service_id: str = None, tx_type: str = "usage",
                                   agent_id: str = None, api_key_id: str = None):
    """Atomically check and deduct credits. Returns (success, balance_after)."""
    async with db.transaction():
        row = await db.fetchrow(
            "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
            user_id
        )
        if not row:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, 100, 100, 'free')
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
            row = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                user_id
            )

        balance = row['credits_balance']
        if balance < cost:
            return False, balance

        new_balance = balance - cost
        await db.execute(
            "UPDATE user_credits SET credits_balance = $1, updated_at = NOW() WHERE user_id = $2::uuid",
            new_balance, user_id
        )
        await db.execute("""
            INSERT INTO credit_transactions
            (user_id, amount, balance_after, type, description, api_endpoint, service_id, agent_id, api_key_id)
            VALUES ($1::uuid, $2, $3, $7, $4, $5, $6, $8, $9::uuid)
        """, user_id, -cost, new_balance, f"API call: {endpoint}", endpoint, service_id, tx_type,
            agent_id, api_key_id)

        return True, new_balance


async def compute_calls_remaining(conn, api_key_id: str) -> int:
    """Single source of truth for calls_remaining. Reads monthly_calls_count directly — never uses credit math."""
    row = await conn.fetchrow(
        "SELECT monthly_calls_count, tier FROM api_keys WHERE id = $1::uuid",
        api_key_id,
    )
    if not row:
        return 0
    p = PLANS.get(row["tier"], PLANS["free"])
    return max(0, p["calls_included"] - row["monthly_calls_count"])


async def _increment_calls(pool, api_key_id: str) -> int:
    """Single increment site for calls_count and monthly_calls_count.
    All call paths (/run, /execute) go through here — nowhere else.
    Returns calls_remaining, or 0 on failure.
    """
    try:
        async with pool.acquire() as _conn:
            row = await _conn.fetchrow(
                "UPDATE api_keys "
                "SET calls_count = calls_count + 1, "
                "    monthly_calls_count = monthly_calls_count + 1, "
                "    monthly_calls_reset_at = COALESCE(monthly_calls_reset_at, "
                "        date_trunc('month', NOW()) + INTERVAL '1 month') "
                "WHERE id = $1::uuid "
                "RETURNING calls_count, monthly_calls_count, tier, user_id",
                api_key_id,
            )
        if row:
            p = PLANS.get(row["tier"], PLANS["free"])
            remaining = max(0, p["calls_included"] - row["monthly_calls_count"])
            logger.info(
                "CALLS_INCREMENT_OK key=%s calls=%s monthly=%s tier=%s remaining=%s",
                api_key_id, row["calls_count"], row["monthly_calls_count"],
                row["tier"], remaining,
            )
            # Fire wayf.balance_low on exactly crossing the 10% threshold
            monthly_limit = p["calls_included"]
            if monthly_limit > 0:
                threshold = monthly_limit * 0.10
                if 0 < remaining < threshold and (remaining + 1) >= threshold:
                    import asyncio as _asyncio
                    _asyncio.create_task(_dispatch_webhooks(
                        str(row["user_id"]), "wayf.balance_low", {
                            "calls_remaining": remaining,
                            "monthly_limit": monthly_limit,
                            "threshold_percent": 10,
                            "tier": row["tier"],
                        }
                    ))
            return remaining
        logger.error("CALLS_INCREMENT_NOMATCH key=%s — UPDATE matched 0 rows", api_key_id)
        return 0
    except Exception as _e:
        logger.error("CALLS_INCREMENT_FAIL key=%s err=%s", api_key_id, _e)
        return 0


async def _dispatch_webhooks(user_id: str, event: str, payload: dict) -> None:
    """Find all active webhooks for this user subscribed to `event`, sign and POST each."""
    import hmac as _hmac
    import json as json_lib
    import time as _time
    from main import app
    pool = app.state.pool
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            owner = await conn.fetchrow(
                "SELECT owner_email FROM api_keys WHERE user_id=$1::uuid AND active=true LIMIT 1",
                user_id,
            )
            if not owner:
                return
            rows = await conn.fetch(
                "SELECT id, webhook_url, secret_token FROM provider_webhooks "
                "WHERE contact_email=$1 AND active=true AND $2=ANY(events)",
                owner["owner_email"], event,
            )
    except Exception as e:
        logger.warning("_dispatch_webhooks db lookup failed: %s", e)
        return

    if not rows:
        return

    timestamp = str(int(_time.time()))
    body = json_lib.dumps(payload)
    async with httpx.AsyncClient(timeout=5.0) as client:
        for row in rows:
            sig = _hmac.new(
                row["secret_token"].encode(),
                f"{timestamp}.{body}".encode(),
                hashlib.sha256,
            ).hexdigest()
            try:
                resp = await client.post(
                    row["webhook_url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Wayforth-Event": event,
                        "X-Wayforth-Timestamp": timestamp,
                        "X-Wayforth-Signature": f"sha256={sig}",
                    },
                )
                logger.info("webhook %s → %s %d", event, row["webhook_url"], resp.status_code)
            except Exception as e:
                logger.warning("webhook delivery failed %s → %s: %s", event, row["webhook_url"], e)
                continue
            try:
                from main import app as _app
                async with _app.state.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE provider_webhooks SET last_fired_at=NOW() WHERE id=$1::uuid",
                        row["id"],
                    )
            except Exception:
                pass


async def _maybe_dispatch_credits_low(pool, user_id: str, api_key_str: str, balance_after: int):
    """Fire credits.low webhook if balance is below the key's topup_trigger_calls threshold."""
    try:
        async with pool.acquire() as db:
            key = await db.fetchrow("""
                SELECT billing_permission, topup_trigger_calls, topup_amount_usd,
                       monthly_topup_limit_usd, monthly_topup_spent_usd
                FROM api_keys WHERE key_hash = $1 AND active = true
            """, hashlib.sha256(api_key_str.encode()).hexdigest())

        if not key:
            return

        threshold_credits = (key["topup_trigger_calls"] or 100) * CREDITS_PER_CALL
        if balance_after >= threshold_credits:
            return

        calls_remaining = balance_after // CREDITS_PER_CALL
        wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
        billing_perm = key["billing_permission"] or "none"

        if billing_perm in ("auto_topup", "full"):
            spent = float(key["monthly_topup_spent_usd"] or 0)
            limit = float(key["monthly_topup_limit_usd"] or 20)
            topup_amt = float(key["topup_amount_usd"] or 5)
            remaining_budget = round(limit - spent, 2)
            base_cred = math.floor(topup_amt * 1000)
            bonus_cred = math.floor(base_cred * 0.05)
            calls_to_receive = (base_cred + bonus_cred) // CREDITS_PER_CALL
            auto_topup_available = remaining_budget >= topup_amt
            topup_instructions = {
                "address": wayforth_wallet,
                "amount_usdc": f"{topup_amt:.6f}",
                "calls_to_receive": calls_to_receive,
                "endpoint": "/billing/topup-usdc",
            }
        else:
            auto_topup_available = False
            topup_instructions = None

        payload: dict = {
            "event": "credits.low",
            "calls_remaining": calls_remaining,
            "billing_permission": billing_perm,
            "auto_topup_available": auto_topup_available,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if auto_topup_available and topup_instructions:
            payload["topup_instructions"] = topup_instructions
        if billing_perm == "none":
            payload["message"] = "Top up at wayforth.io/billing"

        await _dispatch_webhooks(user_id, "credits.low", payload)
    except Exception as _e:
        logger.error("_maybe_dispatch_credits_low error: %s", _e)


async def _downgrade_expired_usdc(api_key_id: str):
    """Gracefully downgrade a USDC subscription that has expired to the free tier."""
    from main import app
    try:
        async with app.state.pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT user_id, tier FROM api_keys WHERE id = $1::uuid", api_key_id
            )
            if not row:
                return
            old_plan = row["tier"]
            await db.execute("""
                UPDATE api_keys
                SET tier = 'free', payment_rail = 'card', subscription_status = 'expired'
                WHERE id = $1::uuid
            """, api_key_id)
            await db.execute("""
                UPDATE user_credits
                SET credits_balance = GREATEST(credits_balance, 600),
                    package_tier = 'free', updated_at = NOW()
                WHERE user_id = $1::uuid
            """, row["user_id"])
            asyncio.create_task(_dispatch_webhooks(
                str(row["user_id"]), "subscription.expired", {
                    "plan": old_plan,
                    "downgraded_to": "free",
                    "message": "Your subscription expired. You're on the free tier (100 calls/month). Renew at wayforth.io/billing",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ))
    except Exception as _e:
        logger.error("_downgrade_expired_usdc error: %s", _e)


async def _monthly_topup_reset():
    """Background task: reset monthly_topup_spent_usd on the first of each month."""
    from main import app
    while True:
        try:
            await asyncio.sleep(3600)  # check hourly
            if not app.state.pool:
                continue
            async with app.state.pool.acquire() as db:
                updated = await db.execute("""
                    UPDATE api_keys
                    SET monthly_topup_spent_usd = 0,
                        monthly_topup_reset_at = date_trunc('month', NOW()) + INTERVAL '1 month'
                    WHERE monthly_topup_reset_at IS NOT NULL
                      AND monthly_topup_reset_at <= NOW()
                """)
                reset_keys = await db.fetch("""
                    SELECT user_id, tier FROM api_keys
                    WHERE monthly_calls_reset_at IS NOT NULL
                      AND monthly_calls_reset_at <= NOW()
                """)
                calls_reset = await db.execute("""
                    UPDATE api_keys
                    SET monthly_calls_count = 0,
                        monthly_calls_reset_at = date_trunc('month', NOW()) + INTERVAL '1 month'
                    WHERE monthly_calls_reset_at IS NOT NULL
                      AND monthly_calls_reset_at <= NOW()
                """)
                wayf_reset = await db.execute("""
                    UPDATE wayf_points
                    SET points_earned_this_month = 0,
                        monthly_points_reset_at = date_trunc('month', NOW()) + INTERVAL '1 month'
                    WHERE monthly_points_reset_at <= NOW()
                    AND points_earned_this_month > 0
                """)
            if updated and updated != "UPDATE 0":
                logger.info("Monthly topup spend reset: %s", updated)
            if calls_reset and calls_reset != "UPDATE 0":
                logger.info("Monthly calls count reset: %s", calls_reset)
                reset_at = datetime.now(timezone.utc).isoformat()
                for _rk in reset_keys:
                    p = PLANS.get(_rk["tier"], PLANS["free"])
                    asyncio.create_task(_dispatch_webhooks(
                        str(_rk["user_id"]), "wayf.calls_reset", {
                            "tier": _rk["tier"],
                            "calls_included": p["calls_included"],
                            "reset_at": reset_at,
                        }
                    ))
        except Exception as _e:
            logger.error("_monthly_topup_reset error: %s", _e)
