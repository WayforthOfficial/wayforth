"""wayf_points.py — $WAYF pre-TGE loyalty points system."""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("wayforth")

# ── Earning constants ─────────────────────────────────────────────────────────

SUBSCRIPTION_POINTS: dict[str, int] = {
    "builder": 50,
    "starter": 120,
    "pro":     400,
    "growth":  1200,
    "free":    0,
}

EXECUTIONS_PER_POINT = 10
DAILY_BONUS_POINTS = 2

MILESTONES: dict[str, int] = {
    "first_execution":         10,
    "first_run_call":          5,
    "first_byok_added":        5,
    "first_webhook":           5,
    "reached_100_executions":  20,
    "reached_1000_executions": 50,
}

MONTHLY_POINTS_CAP = 2000
AIRDROP_POOL_WAYF = 50_000_000
WAYF_CAP = 5000  # hard cap per account

# ── Rate tiers — more users → more points required per $WAYF ─────────────────

RATE_TIERS: list[dict] = [
    {"min_users": 0,      "max_users": 1000,   "points_per_wayf": 10},
    {"min_users": 1000,   "max_users": 2500,   "points_per_wayf": 20},
    {"min_users": 2500,   "max_users": 5000,   "points_per_wayf": 30},
    {"min_users": 5000,   "max_users": 10000,  "points_per_wayf": 40},
    {"min_users": 10000,  "max_users": 20000,  "points_per_wayf": 50},
    {"min_users": 20000,  "max_users": 35000,  "points_per_wayf": 60},
    {"min_users": 35000,  "max_users": 55000,  "points_per_wayf": 70},
    {"min_users": 55000,  "max_users": 80000,  "points_per_wayf": 80},
    {"min_users": 80000,  "max_users": 100000, "points_per_wayf": 90},
    {"min_users": 100000, "max_users": None,   "points_per_wayf": 100},
]

DISCLAIMER = (
    "$WAYF points are a pre-launch loyalty "
    "program. They are not tokens, securities, "
    "or financial instruments. Points may "
    "convert to $WAYF tokens at a future TGE "
    "subject to applicable law, regulatory "
    "approval, and Wayforth terms of service. "
    "Wayforth reserves the right to modify "
    "or terminate this program at any time. "
    "Free tier accounts earn no points."
)


async def get_current_rate(conn) -> dict:
    """Return rate info for the current user count."""
    total_users = int(await conn.fetchval(
        "SELECT COUNT(DISTINCT user_id) FROM wayf_points WHERE points_earned_total > 0"
    ) or 0)
    for i, tier in enumerate(RATE_TIERS):
        max_u = tier["max_users"]
        if max_u is None or total_users < max_u:
            return {
                "points_per_wayf": tier["points_per_wayf"],
                "tier": i + 1,
                "current_users": total_users,
                "next_halving_at": max_u,
                "users_until_next_halving": (max_u - total_users) if max_u else None,
            }
    # Fallback — should never reach here
    return {"points_per_wayf": 100, "tier": 10, "current_users": total_users,
            "next_halving_at": None, "users_until_next_halving": None}


async def _broadcast_rate_change(old_rate: int, new_rate: int, tier: int, total_users: int) -> None:
    """Fire wayf.rate_changed to all subscribed webhooks."""
    import hmac as _hmac
    import time as _time
    import httpx
    from main import app
    pool = getattr(app.state, "pool", None)
    if not pool:
        return
    payload = {
        "event": "wayf.rate_changed",
        "old_rate": old_rate,
        "new_rate": new_rate,
        "tier": tier,
        "total_users": total_users,
        "message": (
            f"The $WAYF earning rate has changed. "
            f"New rate: {new_rate} points = 1 $WAYF. "
            "Your existing $WAYF balance is not affected."
        ),
    }
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, webhook_url, secret_token FROM provider_webhooks "
                "WHERE active = true AND 'wayf.rate_changed' = ANY(events)"
            )
    except Exception as e:
        logger.warning("_broadcast_rate_change db lookup failed: %s", e)
        return

    if not rows:
        return

    timestamp = str(int(_time.time()))
    body = json.dumps(payload)
    async with httpx.AsyncClient(timeout=5.0) as client:
        for row in rows:
            sig = _hmac.new(
                row["secret_token"].encode(),
                f"{timestamp}.{body}".encode(),
                hashlib.sha256,
            ).hexdigest()
            try:
                await client.post(
                    row["webhook_url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Wayforth-Event": "wayf.rate_changed",
                        "X-Wayforth-Timestamp": timestamp,
                        "X-Wayforth-Signature": f"sha256={sig}",
                    },
                )
            except Exception as e:
                logger.warning("wayf.rate_changed webhook failed %s: %s", row["webhook_url"], e)


async def award_points(
    conn, user_id: str, api_key_id: str,
    tier: str, points: int, reason: str, source: str,
    metadata: dict | None = None,
) -> int:
    """Award points at the current rate. Returns actual points awarded."""
    if metadata is None:
        metadata = {}

    cutoff = os.getenv("WAYF_POINTS_CUTOFF_DATE")
    if cutoff:
        cutoff_dt = datetime.fromisoformat(cutoff).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= cutoff_dt:
            return 0

    if tier == "free":
        return 0

    existing = await conn.fetchrow(
        """SELECT points_earned_this_month, monthly_points_reset_at,
                  COALESCE(wayf_balance, 0) AS wayf_balance
           FROM wayf_points WHERE user_id = $1::uuid""",
        user_id,
    )

    now = datetime.now(timezone.utc)
    current_month = existing["points_earned_this_month"] if existing else 0
    reset_at = existing["monthly_points_reset_at"] if existing else None
    existing_wayf = float(existing["wayf_balance"]) if existing else 0.0

    if reset_at and now >= reset_at:
        await conn.execute(
            """UPDATE wayf_points
               SET points_earned_this_month = 0,
                   monthly_points_reset_at = date_trunc('month', NOW()) + INTERVAL '1 month'
               WHERE user_id = $1::uuid""",
            user_id,
        )
        current_month = 0

    remaining = max(0, MONTHLY_POINTS_CAP - current_month)
    actual = min(points, remaining)
    if actual <= 0:
        return 0

    # Lock in the rate at the moment of award
    rate_info = await get_current_rate(conn)
    current_rate = rate_info["points_per_wayf"]

    wayf_earned_raw = actual / current_rate
    wayf_earned = min(wayf_earned_raw, max(0.0, float(WAYF_CAP) - existing_wayf))

    await conn.execute(
        """
        INSERT INTO wayf_points (
            user_id, api_key_id,
            points_balance, points_earned_total, points_earned_this_month,
            monthly_points_reset_at, wayf_balance
        ) VALUES (
            $1::uuid, $2::uuid, $3, $3, $3,
            date_trunc('month', NOW()) + INTERVAL '1 month',
            $4
        )
        ON CONFLICT (user_id) DO UPDATE SET
            points_balance           = wayf_points.points_balance + $3,
            points_earned_total      = wayf_points.points_earned_total + $3,
            points_earned_this_month = wayf_points.points_earned_this_month + $3,
            monthly_points_reset_at  = COALESCE(
                wayf_points.monthly_points_reset_at,
                date_trunc('month', NOW()) + INTERVAL '1 month'
            ),
            wayf_balance = LEAST(
                wayf_points.wayf_balance + $4,
                $5
            ),
            updated_at = NOW()
        """,
        user_id, api_key_id, actual, wayf_earned, float(WAYF_CAP),
    )

    await conn.execute(
        """INSERT INTO wayf_points_log
               (user_id, api_key_id, points, reason, source, metadata, rate_at_award)
           VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)""",
        user_id, api_key_id, actual, reason, source, json.dumps(metadata), current_rate,
    )

    # Detect tier crossing: re-fetch count; if rate changed, broadcast
    rate_info_after = await get_current_rate(conn)
    if rate_info_after["points_per_wayf"] != current_rate:
        asyncio.create_task(_broadcast_rate_change(
            current_rate,
            rate_info_after["points_per_wayf"],
            rate_info_after["tier"],
            rate_info_after["current_users"],
        ))

    return actual


async def check_milestones(
    conn, user_id: str, api_key_id: str, tier: str, calls_count: int
) -> None:
    """Award one-time milestone points based on cumulative call count."""
    earned_raw = await conn.fetchval(
        "SELECT array_agg(reason) FROM wayf_points_log WHERE user_id = $1::uuid AND source = 'milestone'",
        user_id,
    )
    earned = set(earned_raw or [])

    checks = [
        ("first_execution", 1),
        ("reached_100_executions", 100),
        ("reached_1000_executions", 1000),
    ]
    for key, threshold in checks:
        if key not in earned and calls_count >= threshold:
            await award_points(
                conn, user_id, api_key_id, tier,
                MILESTONES[key], key, "milestone",
                {"calls_at_milestone": calls_count},
            )
