import asyncio
import hashlib
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("wayforth")

ROUTING_FEE = 0.015  # 1.5% flat, all tiers

# Blended average for DISPLAY purposes only — never used for billing logic
CREDITS_PER_CALL = 6

PAYMENT_MULTIPLIERS: dict[str, float] = {
    "card": 1.00,
    "usdc": 1.05,
}

PLANS = {
    "free": {
        "monthly_credits":    100,
        "calls_included":     100,   # credits per month (was call-count; now equals monthly_credits)
        "price_usd":          0,
        "price_usdc":         0,
        "usdc_bonus_credits": 0,
        "stripe_price_env":   None,
        "features":           ["search", "execute", "wayforthrank"],
    },
    "builder": {
        "monthly_credits":    6_000,
        "calls_included":     6_000,
        "price_usd":          12,
        "price_usdc":         12,
        "usdc_bonus_credits": 300,    # 5%
        "stripe_price_env":   "STRIPE_PRICE_BUILDER",
        "features":           ["search", "execute", "wayforthrank", "byok", "webhooks"],
    },
    "starter": {
        "monthly_credits":    21_000,
        "calls_included":     21_000,
        "price_usd":          29,
        "price_usdc":         29,
        "usdc_bonus_credits": 1_050,  # 5%
        "stripe_price_env":   "STRIPE_PRICE_STARTER",
        "features":           ["builder_features", "analytics", "wayforthql"],
    },
    "pro": {
        "monthly_credits":    72_000,
        "calls_included":     72_000,
        "price_usd":          99,
        "price_usdc":         99,
        "usdc_bonus_credits": 3_600,   # 5%
        "stripe_price_env":   "STRIPE_PRICE_PRO",
        "features":           ["starter_features", "wri_scores", "priority"],
    },
    "growth": {
        "monthly_credits":    240_000,
        "calls_included":     240_000,
        "price_usd":          299,
        "price_usdc":         299,
        "usdc_bonus_credits": 12_000,  # 5%
        "stripe_price_env":   "STRIPE_PRICE_GROWTH",
        "features":           ["pro_features", "custom_services", "no_limits"],
    },
    "enterprise": {
        "monthly_credits":    1_000_000,
        "calls_included":     1_000_000,
        "price_usd":          0,
        "price_usdc":         0,
        "usdc_bonus_credits": 0,
        "stripe_price_env":   None,
        "features":           ["growth_features", "custom_services", "no_rate_limits", "priority_support", "sla"],
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

# Annual plans: 10 months price (2 months free). Credits replenished monthly.
PLAN_ANNUAL_DETAILS: dict[str, dict] = {
    "builder": {"price_usd_annual": 99.0,    "credits": 6_000,   "savings_usd": 45.0},
    "starter": {"price_usd_annual": 290.0,   "credits": 21_000,  "savings_usd": 58.0},
    "pro":     {"price_usd_annual": 990.0,   "credits": 72_000,  "savings_usd": 198.0},
    "growth":  {"price_usd_annual": 2_990.0, "credits": 240_000, "savings_usd": 598.0},
}

_PLAN_ANNUAL_PRICE_ENV: dict[str, str] = {
    "builder": "STRIPE_PRICE_BUILDER_ANNUAL",
    "starter": "STRIPE_PRICE_STARTER_ANNUAL",
    "pro":     "STRIPE_PRICE_PRO_ANNUAL",
    "growth":  "STRIPE_PRICE_GROWTH_ANNUAL",
}

# Growth-tier credit value (binding constraint for margin alerts).
# Growth: $299/mo × 12 / (240_000 credits × 12) = $0.001246/credit net to Wayforth.
_GROWTH_CREDIT_VALUE_USD = 0.001246

def x402_developer_charge(provider_price: float) -> float:
    """Return the amount to charge the developer for an x402 call.

    Developer pays provider_price * 1.015.
    Provider receives provider_price exactly (100%).
    Wayforth keeps 1.5% as a routing/discovery fee.
    Fee is charged to the developer, not deducted from the provider.
    """
    return round(provider_price * (1 + ROUTING_FEE), 8)


CREDIT_COSTS = {
    "search": 1,
    "query": 2,
    "intelligence": 5,
    "graph": 2,
    "wri_history": 1,
    "payment_routing": 100,  # per $1 routed
}


def check_service_margins() -> None:
    """Warn at startup if any managed service margin falls below $0.005 at Growth tier."""
    from services.managed import SERVICE_CONFIGS
    for slug, cfg in SERVICE_CONFIGS.items():
        credits = cfg.get("credits", 1)
        api_cost = cfg.get("real_cost_per_call", 0.0)
        margin = credits * _GROWTH_CREDIT_VALUE_USD - api_cost
        if margin < 0.005:
            logger.warning(
                "MARGIN ALERT: %s margin=$%.4f at Growth tier (credits=%d, api_cost=$%.4f)",
                slug, margin, credits, api_cost,
            )


async def check_and_deduct_credits(db, user_id: str, cost: int, endpoint: str,
                                   service_id: str = None, tx_type: str = "execution",
                                   agent_id: str = None, api_key_id: str = None,
                                   return_tx_id: bool = False):
    """Atomically check and deduct credits.

    Returns (success, balance_after) normally, or (success, balance_after, tx_id)
    when return_tx_id=True. Callers that need the transaction ID for post-call
    signal enrichment should pass return_tx_id=True.
    """
    async with db.transaction():
        row = await db.fetchrow(
            "SELECT credits_balance, pioneer_credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
            user_id
        )
        if not row:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, 100, 100, 'free')
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
            row = await db.fetchrow(
                "SELECT credits_balance, pioneer_credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                user_id
            )

        balance = row['credits_balance']
        pioneer = row['pioneer_credits_balance']
        # Spend the main balance first, then draw from the pioneer overflow pool.
        if balance + pioneer < cost:
            if return_tx_id:
                return False, balance, None
            return False, balance

        if balance >= cost:
            new_balance, new_pioneer = balance - cost, pioneer
        else:
            new_balance, new_pioneer = 0, pioneer - (cost - balance)
        await db.execute(
            "UPDATE user_credits SET credits_balance = $1, pioneer_credits_balance = $2, "
            "updated_at = NOW() WHERE user_id = $3::uuid",
            new_balance, new_pioneer, user_id
        )
        # balance_after records the MAIN pool after deduction (unchanged semantics).
        tx_id = await db.fetchval("""
            INSERT INTO credit_transactions
            (user_id, amount, balance_after, type, description, api_endpoint, service_id, agent_id, api_key_id)
            VALUES ($1::uuid, $2, $3, $7, $4, $5, $6, $8, $9::uuid)
            RETURNING id
        """, user_id, -cost, new_balance, f"API call: {endpoint}", endpoint, service_id, tx_type,
            agent_id, api_key_id)

        if return_tx_id:
            return True, new_balance, tx_id
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


async def _maybe_send_usage_warning_email(pool, user_id: str, calls_remaining: int, percent_used: int, tier: str) -> None:
    try:
        async with pool.acquire() as _conn:
            row = await _conn.fetchrow(
                """SELECT u.email, ak.monthly_calls_reset_at
                   FROM users u
                   JOIN api_keys ak ON ak.user_id = u.id
                   WHERE u.id = $1::uuid AND ak.active = true LIMIT 1""",
                user_id,
            )
        if row and row["email"]:
            reset_dt = row["monthly_calls_reset_at"]
            reset_date = reset_dt.strftime("%B %d") if reset_dt else "next month"
            import asyncio as _asyncio
            await _asyncio.to_thread(
                _send_usage_warning_email, row["email"], calls_remaining, percent_used, tier, reset_date
            )
    except Exception as _e:
        logger.warning("_maybe_send_usage_warning_email error: %s", _e)


def _send_usage_warning_email(to_email: str, calls_remaining: int, percent_used: int, tier: str, reset_date: str) -> None:
    try:
        from notifications import send_usage_warning_email
        send_usage_warning_email(to_email, calls_remaining, percent_used, tier, reset_date)
    except Exception as _e:
        logger.warning("send_usage_warning_email error: %s", _e)


async def _send_webhook_suspension_email(contact_email: str, webhook_url: str, webhook_id: str) -> None:
    if not contact_email:
        return
    try:
        import asyncio as _asyncio
        await _asyncio.to_thread(_do_send_webhook_suspension_email, contact_email, webhook_url, webhook_id)
    except Exception as _e:
        logger.warning("_send_webhook_suspension_email error: %s", _e)


def _do_send_webhook_suspension_email(contact_email: str, webhook_url: str, webhook_id: str) -> None:
    try:
        from notifications import send_webhook_suspension_email
        send_webhook_suspension_email(contact_email, webhook_url, webhook_id)
    except Exception as _e:
        logger.warning("send_webhook_suspension_email error: %s", _e)


async def _increment_calls(pool, api_key_id: str, cost: int = 1) -> int:
    """Increment call counter and credit-usage tracker after a successful execution.

    `cost` is the credit cost of the call (e.g. 3 for groq, 20 for deepl).
    monthly_calls_count now tracks credits consumed this month, not raw call count,
    so compute_calls_remaining() returns the correct credit-based balance.

    Returns credits_remaining, or 0 on failure.
    """
    try:
        async with pool.acquire() as _conn:
            row = await _conn.fetchrow(
                "UPDATE api_keys "
                "SET calls_count = calls_count + 1, "
                "    monthly_calls_count = monthly_calls_count + $2, "
                "    monthly_calls_reset_at = COALESCE(monthly_calls_reset_at, "
                "        date_trunc('month', NOW()) + INTERVAL '1 month') "
                "WHERE id = $1::uuid "
                "RETURNING calls_count, monthly_calls_count, tier, user_id",
                api_key_id, cost,
            )
        if row:
            p = PLANS.get(row["tier"], PLANS["free"])
            remaining = max(0, p["calls_included"] - row["monthly_calls_count"])
            logger.info(
                "CALLS_INCREMENT_OK key=%s calls=%s monthly=%s tier=%s remaining=%s",
                api_key_id, row["calls_count"], row["monthly_calls_count"],
                row["tier"], remaining,
            )
            # Fire usage alerts on threshold crossings (fires exactly once per crossing)
            monthly_limit = p["calls_included"]
            if monthly_limit > 0:
                import asyncio as _asyncio
                # 80% used — 20% remaining
                t80 = monthly_limit * 0.20
                if 0 < remaining < t80 and (remaining + 1) >= t80:
                    _asyncio.create_task(_dispatch_webhooks(
                        str(row["user_id"]), "wayf.balance_warning_80", {
                            "credits_remaining": remaining,
                            "calls_remaining": remaining,  # backward compat
                            "monthly_limit": monthly_limit,
                            "threshold_percent": 80,
                            "tier": row["tier"],
                        }
                    ))
                    _asyncio.create_task(_maybe_send_usage_warning_email(
                        pool, str(row["user_id"]), remaining, 80, row["tier"]
                    ))
                # 90% used — 10% remaining (existing wayf.balance_low)
                t10 = monthly_limit * 0.10
                if 0 < remaining < t10 and (remaining + 1) >= t10:
                    _asyncio.create_task(_dispatch_webhooks(
                        str(row["user_id"]), "wayf.balance_low", {
                            "credits_remaining": remaining,
                            "calls_remaining": remaining,  # backward compat
                            "monthly_limit": monthly_limit,
                            "threshold_percent": 10,
                            "tier": row["tier"],
                        }
                    ))
                # 95% used — 5% remaining
                t5 = monthly_limit * 0.05
                if 0 < remaining < t5 and (remaining + 1) >= t5:
                    _asyncio.create_task(_dispatch_webhooks(
                        str(row["user_id"]), "wayf.balance_warning_95", {
                            "credits_remaining": remaining,
                            "calls_remaining": remaining,  # backward compat
                            "monthly_limit": monthly_limit,
                            "threshold_percent": 95,
                            "tier": row["tier"],
                        }
                    ))
                    _asyncio.create_task(_maybe_send_usage_warning_email(
                        pool, str(row["user_id"]), remaining, 95, row["tier"]
                    ))
                # 100% used — zero remaining
                if remaining == 0:
                    _asyncio.create_task(_dispatch_webhooks(
                        str(row["user_id"]), "credits.exhausted", {
                            "credits_remaining": 0,
                            "calls_remaining": 0,  # backward compat
                            "monthly_limit": monthly_limit,
                            "tier": row["tier"],
                        }
                    ))
            return remaining
        logger.error("CALLS_INCREMENT_NOMATCH key=%s — UPDATE matched 0 rows", api_key_id)
        return 0
    except Exception as _e:
        logger.error("CALLS_INCREMENT_FAIL key=%s err=%s", api_key_id, _e)
        return 0


_RETRY_DELAYS_SEC = [60, 300, 1800, 7200]  # 1m, 5m, 30m, 2h after each failed attempt


async def _dispatch_webhooks(user_id: str, event: str, payload: dict) -> None:
    """Find all active webhooks for this user subscribed to `event`, sign and POST each.

    Attempts are recorded in webhook_deliveries. Failures schedule retries via
    _webhook_retry_loop at 1m, 5m, 30m, 2h intervals; dead after 5 total attempts.
    """
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
    # follow_redirects defaults to False in httpx but pin explicitly so a future
    # default change can't reintroduce SSRF via 30x to internal hosts.
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        for row in rows:
            # Re-validate the destination URL at dispatch time. URLs were
            # validated at registration but a hostname's A record can change
            # between then and now (DNS rebinding, attacker-controlled DNS).
            # This re-resolves and refuses delivery to internal / loopback /
            # link-local / metadata addresses.
            try:
                from core.url_validation import validate_external_url
                validate_external_url(row["webhook_url"], field_name="url")
            except Exception as _vexc:
                logger.warning(
                    "webhook delivery refused: url=%s — %s",
                    row["webhook_url"], _vexc,
                )
                continue
            sig = _hmac.new(
                row["secret_token"].encode(),
                f"{timestamp}.{body}".encode(),
                hashlib.sha256,
            ).hexdigest()

            # Create delivery record before attempt
            delivery_id: str | None = None
            try:
                async with pool.acquire() as dconn:
                    dr = await dconn.fetchrow(
                        """INSERT INTO webhook_deliveries
                           (webhook_id, user_id, event, payload, attempt, status, last_attempted_at)
                           VALUES ($1::uuid, $2::uuid, $3, $4, 1, 'pending', NOW())
                           RETURNING id""",
                        str(row["id"]), user_id, event, body,
                    )
                    delivery_id = str(dr["id"]) if dr else None
            except Exception as ins_err:
                logger.warning("webhook delivery insert failed: %s", ins_err)

            success = False
            status_code: int | None = None
            error: str | None = None
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
                status_code = resp.status_code
                success = resp.status_code < 300
                logger.info("webhook %s → %s %d", event, row["webhook_url"], resp.status_code)
            except Exception as e:
                error = str(e)[:200]
                logger.warning("webhook delivery failed %s → %s: %s", event, row["webhook_url"], e)

            # Update delivery record with result
            if delivery_id:
                try:
                    async with pool.acquire() as upd:
                        if success:
                            await upd.execute(
                                "UPDATE webhook_deliveries SET status='delivered', "
                                "response_status=$1 WHERE id=$2::uuid",
                                status_code, delivery_id,
                            )
                            await upd.execute(
                                "UPDATE provider_webhooks SET last_fired_at=NOW() WHERE id=$1::uuid",
                                str(row["id"]),
                            )
                        else:
                            next_retry = datetime.now(timezone.utc) + timedelta(seconds=_RETRY_DELAYS_SEC[0])
                            await upd.execute(
                                "UPDATE webhook_deliveries SET response_status=$1, error=$2, "
                                "next_retry_at=$3 WHERE id=$4::uuid",
                                status_code, error, next_retry, delivery_id,
                            )
                except Exception as upd_err:
                    logger.warning("webhook delivery update failed: %s", upd_err)
            elif success:
                try:
                    async with pool.acquire() as upd:
                        await upd.execute(
                            "UPDATE provider_webhooks SET last_fired_at=NOW() WHERE id=$1::uuid",
                            str(row["id"]),
                        )
                except Exception:
                    pass  # non-critical: last_fired_at timestamp update; webhook was still sent


# Per-user cooldown for spend anomaly alerts — avoids repeated fires within 1 hour
_spend_anomaly_cooldown: dict[str, float] = {}
_ANOMALY_COOLDOWN_SEC = 3600


async def _check_spend_anomaly(pool, user_id: str) -> None:
    """Fire wayf.spend_anomaly webhook + email if 1-hour spend > 3× 7-day daily average.
    Does not block the account. Silenced for 1 hour after each fire per user.
    """
    import time as _t
    now = _t.time()
    if now - _spend_anomaly_cooldown.get(user_id, 0) < _ANOMALY_COOLDOWN_SEC:
        return
    try:
        async with pool.acquire() as conn:
            spend_1h: float = await conn.fetchval("""
                SELECT COALESCE(SUM(ABS(amount)), 0)
                FROM credit_transactions
                WHERE user_id = $1::uuid AND type = 'execution' AND amount < 0
                  AND created_at > NOW() - INTERVAL '1 hour'
            """, user_id) or 0
            daily_avg_7d: float = await conn.fetchval("""
                SELECT COALESCE(SUM(ABS(amount)), 0) / 7.0
                FROM credit_transactions
                WHERE user_id = $1::uuid AND type = 'execution' AND amount < 0
                  AND created_at > NOW() - INTERVAL '7 days'
            """, user_id) or 0.0
            if daily_avg_7d < 6 or spend_1h <= 3 * daily_avg_7d:
                return
            _spend_anomaly_cooldown[user_id] = now
            user_row = await conn.fetchrow(
                "SELECT u.email FROM users u "
                "JOIN api_keys k ON k.user_id = u.id "
                "WHERE u.id = $1::uuid AND k.active = true LIMIT 1",
                user_id,
            )
            user_email = user_row["email"] if user_row else None
    except Exception as _e:
        logger.warning("_check_spend_anomaly error: %s", _e)
        return

    ratio = round(spend_1h / daily_avg_7d, 2)
    logger.warning("SPEND_ANOMALY user=%s spend_1h=%d daily_avg=%.1f ratio=%.2f", user_id, spend_1h, daily_avg_7d, ratio)
    asyncio.create_task(_dispatch_webhooks(user_id, "wayf.spend_anomaly", {
        "user_id": user_id,
        "spend_1h_credits": int(spend_1h),
        "daily_avg_7d_credits": round(daily_avg_7d, 1),
        "ratio": ratio,
        "threshold": 3.0,
        "action": "alert_only",
    }))
    if user_email:
        asyncio.create_task(asyncio.to_thread(
            _send_spend_anomaly_email_sync, user_email, int(spend_1h), round(daily_avg_7d, 1)
        ))


def _send_spend_anomaly_email_sync(to_email: str, spend_1h: int, daily_avg: float) -> None:
    try:
        from notifications import send_spend_anomaly_email
        send_spend_anomaly_email(to_email, spend_1h, daily_avg)
    except Exception as _e:
        logger.warning("send_spend_anomaly_email error: %s", _e)


async def _webhook_retry_loop() -> None:
    """Background task: retry pending webhook deliveries with exponential backoff.

    Picks up deliveries where next_retry_at <= now, retries up to 5 total attempts,
    then marks the delivery dead.

    v0.8.0 Item 5: rows now carry a `kind` column. For kind='generic' the JOIN
    against provider_webhooks resolves webhook_url + secret_token (existing
    path). For kind='wri_alert' the row carries notify_url + hmac_secret
    directly, so a deactivated WRI alert doesn't strand its in-flight
    deliveries.
    """
    import hmac as _hmac
    import time as _time
    from main import app
    await asyncio.sleep(30)
    while True:
        pool = getattr(app.state, "pool", None)
        if pool:
            try:
                async with pool.acquire() as conn:
                    due = await conn.fetch("""
                        SELECT wd.id, wd.webhook_id, wd.event, wd.payload,
                               wd.attempt, wd.kind, wd.notify_url, wd.hmac_secret,
                               wd.source_id,
                               pw.webhook_url, pw.secret_token, pw.contact_email
                        FROM webhook_deliveries wd
                        LEFT JOIN provider_webhooks pw
                               ON pw.id = wd.webhook_id AND pw.active = true
                        WHERE wd.status = 'pending'
                          AND wd.next_retry_at IS NOT NULL
                          AND wd.next_retry_at <= NOW()
                        LIMIT 50
                    """)

                if due:
                    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                        for row in due:
                            # Resolve destination + secret by kind. Generic rows
                            # use the LEFT-joined provider_webhooks values; WRI
                            # alert rows carry their own notify_url and hmac_secret.
                            row_kind = row["kind"] or "generic"
                            if row_kind == "wri_alert":
                                target_url = row["notify_url"]
                                secret = row["hmac_secret"]
                                contact_email = None
                            else:
                                # kind='generic' requires the LEFT JOIN to have
                                # resolved (i.e. the webhook is still active).
                                if not row["webhook_url"]:
                                    # provider_webhooks row missing or inactive
                                    # — skip; the original deactivation already
                                    # logged a reason.
                                    continue
                                target_url = row["webhook_url"]
                                secret = row["secret_token"]
                                contact_email = row.get("contact_email")

                            # Re-validate destination on every retry — see
                            # _dispatch_webhooks above for rationale.
                            try:
                                from core.url_validation import validate_external_url
                                validate_external_url(target_url, field_name="url")
                            except Exception as _vexc:
                                logger.warning(
                                    "webhook retry refused: kind=%s url=%s — %s",
                                    row_kind, target_url, _vexc,
                                )
                                continue
                            ts = str(int(_time.time()))
                            body = row["payload"]
                            sig = _hmac.new(
                                (secret or "").encode(),
                                f"{ts}.{body}".encode(),
                                hashlib.sha256,
                            ).hexdigest()
                            success = False
                            status_code: int | None = None
                            error: str | None = None
                            try:
                                resp = await client.post(
                                    target_url,
                                    content=body,
                                    headers={
                                        "Content-Type": "application/json",
                                        "X-Wayforth-Event": row["event"],
                                        "X-Wayforth-Timestamp": ts,
                                        "X-Wayforth-Signature": f"sha256={sig}",
                                    },
                                )
                                status_code = resp.status_code
                                success = resp.status_code < 300
                            except Exception as e:
                                error = str(e)[:200]

                            new_attempt = row["attempt"] + 1
                            async with pool.acquire() as upd:
                                if success:
                                    await upd.execute(
                                        "UPDATE webhook_deliveries SET status='delivered', "
                                        "last_attempted_at=NOW(), attempt=$1, response_status=$2 "
                                        "WHERE id=$3::uuid",
                                        new_attempt, status_code, str(row["id"]),
                                    )
                                    # Bookkeeping per kind. Generic webhooks bump
                                    # provider_webhooks.last_fired_at; WRI alerts
                                    # log to wri_alert_logs.
                                    if row_kind == "wri_alert":
                                        await upd.execute(
                                            "INSERT INTO wri_alert_logs "
                                            "(alert_id, service_slug, old_wri, new_wri, "
                                            " fired_at, response_status, success) "
                                            "VALUES ($1, '', NULL, NULL, NOW(), $2, true)",
                                            row["source_id"], status_code,
                                        )
                                    elif row["webhook_id"]:
                                        await upd.execute(
                                            "UPDATE provider_webhooks SET last_fired_at=NOW() "
                                            "WHERE id=$1::uuid",
                                            str(row["webhook_id"]),
                                        )
                                elif new_attempt > 5:
                                    await upd.execute(
                                        "UPDATE webhook_deliveries SET status='dead', "
                                        "last_attempted_at=NOW(), attempt=$1, "
                                        "response_status=$2, error=$3 WHERE id=$4::uuid",
                                        new_attempt, status_code, error, str(row["id"]),
                                    )
                                    if row_kind == "wri_alert":
                                        await upd.execute(
                                            "INSERT INTO wri_alert_logs "
                                            "(alert_id, service_slug, old_wri, new_wri, "
                                            " fired_at, response_status, success) "
                                            "VALUES ($1, '', NULL, NULL, NOW(), $2, false)",
                                            row["source_id"], status_code,
                                        )
                                        # No suspension email — WRI alerts have
                                        # no per-user contact stored in
                                        # webhook_deliveries; the alert owner can
                                        # see the failure via the wri_alert_logs.
                                    elif row["webhook_id"]:
                                        await upd.execute(
                                            "UPDATE provider_webhooks SET suspended_at=NOW() "
                                            "WHERE id=$1::uuid",
                                            str(row["webhook_id"]),
                                        )
                                        asyncio.create_task(_send_webhook_suspension_email(
                                            row.get("contact_email") or "",
                                            str(row.get("webhook_url") or ""),
                                            str(row["webhook_id"]),
                                        ))
                                else:
                                    delay = _RETRY_DELAYS_SEC[min(new_attempt - 2, len(_RETRY_DELAYS_SEC) - 1)]
                                    next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
                                    await upd.execute(
                                        "UPDATE webhook_deliveries SET attempt=$1, "
                                        "last_attempted_at=NOW(), next_retry_at=$2, "
                                        "response_status=$3, error=$4 WHERE id=$5::uuid",
                                        new_attempt, next_retry, status_code, error, str(row["id"]),
                                    )
            except Exception as e:
                logger.warning("webhook retry loop error: %s", e)
        await asyncio.sleep(60)


async def _maybe_dispatch_credits_low(pool, user_id: str, api_key_str: str, balance_after: int):
    """Fire credits.low webhook if balance is below the key's topup_trigger_calls threshold."""
    try:
        async with pool.acquire() as db:
            key = await db.fetchrow("""
                SELECT billing_permission, topup_trigger_calls, topup_amount_usd,
                       monthly_topup_limit_usd, monthly_topup_spent_usd, tier
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
            "credits_remaining": calls_remaining,
            "calls_remaining": calls_remaining,  # backward compat
            "billing_permission": billing_perm,
            "auto_topup_available": auto_topup_available,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if auto_topup_available and topup_instructions:
            payload["topup_instructions"] = topup_instructions
        if billing_perm == "none":
            payload["message"] = "Top up at wayforth.io/billing"

        await _dispatch_webhooks(user_id, "credits.low", payload)

        # Transactional email alert
        try:
            async with pool.acquire() as _email_db:
                user_row = await _email_db.fetchrow(
                    """SELECT u.email, ak.monthly_calls_reset_at
                       FROM users u
                       JOIN api_keys ak ON ak.user_id = u.id
                       WHERE u.id = $1::uuid AND ak.active = true LIMIT 1""",
                    user_id,
                )
            if user_row and user_row["email"]:
                from core.email import send_email, _build_upgrade_cta
                tier = key["tier"] or "free"
                plan_calls = PLANS.get(tier, PLANS["free"])["calls_included"]
                percent_remaining = round(calls_remaining / plan_calls * 100) if plan_calls > 0 else 0
                reset_dt = user_row["monthly_calls_reset_at"]
                reset_date = reset_dt.strftime("%B %d") if reset_dt else "next month"
                asyncio.create_task(send_email(user_row["email"], "low_credits", {
                    "credits_remaining": str(calls_remaining),
                    "percent_remaining": str(percent_remaining),
                    "renewal_date": reset_date,
                    "upgrade_cta": _build_upgrade_cta(tier),
                }))
        except Exception as _email_err:
            logger.warning("credits_low email dispatch error: %s", _email_err)
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
                    "message": "Your subscription expired. You're on the free tier (100 credits/month). Renew at wayforth.io/billing",
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
                # DISTINCT ON (user_id) prevents double-crediting users who have
                # multiple API keys with different monthly_calls_reset_at dates.
                # active DESC ensures the active key's tier wins when a user has
                # both active and inactive keys in the qualifying set.
                reset_keys = await db.fetch("""
                    SELECT DISTINCT ON (ak.user_id) ak.user_id, ak.tier
                      FROM api_keys ak
                     WHERE ak.monthly_calls_reset_at IS NOT NULL
                       AND ak.monthly_calls_reset_at <= NOW()
                       AND ak.user_id IS NOT NULL
                     ORDER BY ak.user_id,
                              ak.active DESC,
                              ak.monthly_calls_reset_at
                """)
                calls_reset = await db.execute("""
                    UPDATE api_keys
                    SET monthly_calls_count = 0,
                        monthly_calls_reset_at = date_trunc('month', NOW()) + INTERVAL '1 month'
                    WHERE monthly_calls_reset_at IS NOT NULL
                      AND monthly_calls_reset_at <= NOW()
                """)
                # Replenish credits_balance to the plan's monthly_credits allowance.
                # GREATEST() preserves USDC prepay and pioneer drip accruals above plan_max.
                # last_credited_at gate: idempotent — skips if already credited this
                # calendar month, closing the multi-key replenishment vector.
                for _rk in reset_keys:
                    _p = PLANS.get(_rk["tier"], PLANS["free"])
                    _monthly = _p["monthly_credits"]
                    await db.execute("""
                        UPDATE user_credits
                           SET credits_balance  = GREATEST(credits_balance, $1),
                               pioneer_credits_balance = 0,
                               last_credited_at = NOW(),
                               updated_at       = NOW()
                         WHERE user_id = $2::uuid
                           AND (
                               last_credited_at IS NULL
                               OR date_trunc('month', last_credited_at AT TIME ZONE 'UTC')
                                  < date_trunc('month', NOW() AT TIME ZONE 'UTC')
                           )
                    """, _monthly, _rk["user_id"])
                    # Zero per-cycle pioneer drip counters so the dashboard shows
                    # "earned this cycle" from day 1 of the new subscription month.
                    # Lifetime enrollment days are derived at query time from
                    # pioneer_opted_in_at and are never reset.
                    await db.execute("""
                        UPDATE users
                           SET pioneer_drip_credits_this_cycle = 0,
                               pioneer_drip_days_this_cycle    = 0
                         WHERE id = $1::uuid
                    """, _rk["user_id"])
            if updated and updated != "UPDATE 0":
                logger.info("Monthly topup spend reset: %s", updated)
            if calls_reset and calls_reset != "UPDATE 0":
                logger.info("Monthly calls count reset: %s", calls_reset)
                reset_at = datetime.now(timezone.utc).isoformat()
                for _rk in reset_keys:
                    p = PLANS.get(_rk["tier"], PLANS["free"])
                    asyncio.create_task(_dispatch_webhooks(
                        str(_rk["user_id"]), "wayf.credits_reset", {
                            "tier": _rk["tier"],
                            "credits_included": p["calls_included"],
                            "calls_included": p["calls_included"],  # backward compat
                            "reset_at": reset_at,
                        }
                    ))
        except Exception as _e:
            logger.error("_monthly_topup_reset error: %s", _e)


async def _maybe_grant_founding_bonus(db, user_id: str) -> bool:
    """Grant 500 founding-member bonus credits on a user's first paid invoice.

    Safe to call after any payment; no-ops if already granted or not a founding member.
    Returns True if the bonus was granted this call.
    """
    user = await db.fetchrow(
        "SELECT founding_member, founding_bonus_granted_at FROM users WHERE id = $1::uuid",
        user_id,
    )
    if not user or not user["founding_member"] or user["founding_bonus_granted_at"] is not None:
        return False

    async with db.transaction():
        await db.execute("""
            UPDATE user_credits
            SET credits_balance    = credits_balance    + 500,
                lifetime_credits   = lifetime_credits   + 500
            WHERE user_id = $1::uuid
        """, user_id)
        await db.execute(
            "UPDATE users SET founding_bonus_granted_at = NOW() WHERE id = $1::uuid",
            user_id,
        )

    asyncio.create_task(_dispatch_webhooks(user_id, "wayf.founding_bonus_granted", {
        "event": "wayf.founding_bonus_granted",
        "bonus_credits": 500,
        "user_id": user_id,
    }))
    logger.info("founding_bonus_granted user_id=%s", user_id)

    try:
        email_row = await db.fetchrow(
            "SELECT email FROM users WHERE id = $1::uuid", user_id
        )
        if email_row and email_row["email"]:
            from core.email import send_email
            asyncio.create_task(send_email(email_row["email"], "founding_member", {
                "bonus_credits": "500",
                "cutoff_date": "August 31, 2026",
            }))
    except Exception as _em:
        logger.warning("founding_member email error: %s", _em)

    return True
