"""tier_gates.py — Feature availability and per-tier rate limits."""
import logging
import os
import time
import uuid as _uuid_mod

from fastapi import HTTPException

logger = logging.getLogger("wayforth")

TIER_FEATURES: dict[str, list[str]] = {
    "search":             ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "execute_managed":    ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "run":                ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "leaderboard":        ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "status":             ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "account_balance":    ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "byok":               ["builder", "starter", "pro", "growth", "enterprise"],
    "webhooks":           ["builder", "starter", "pro", "growth", "enterprise"],
    "agent_id_tagging":   ["builder", "starter", "pro", "growth", "enterprise"],
    "account_agents":     ["builder", "starter", "pro", "growth", "enterprise"],
    "wri_alerts":         ["builder", "starter", "pro", "growth", "enterprise"],
    "topup_usdc":         ["builder", "starter", "pro", "growth", "enterprise"],
    "compare":            ["starter", "pro", "growth", "enterprise"],
    "analytics":          ["starter", "pro", "growth", "enterprise"],
    "wayforthql":         ["starter", "pro", "growth", "enterprise"],
    "wri_scores_visible": ["pro", "growth", "enterprise"],
    "priority_execution": ["pro", "growth", "enterprise"],
    "agent_identity":     ["pro", "growth", "enterprise"],
    "custom_services":    ["growth", "enterprise"],
    "no_rate_limits":     ["growth", "enterprise"],
    "priority_support":   ["enterprise"],
}

TIER_RATE_LIMITS: dict[str, dict] = {
    "free":       {"calls_per_minute": 15,  "calls_per_hour": 100},
    "builder":    {"calls_per_minute": 120, "calls_per_hour": 500},
    "starter":    {"calls_per_minute": 300, "calls_per_hour": 1500},
    "pro":        {"calls_per_minute": 600, "calls_per_hour": 5000},
    "growth":     {"calls_per_minute": 600, "calls_per_hour": 10000},
    "enterprise": {"calls_per_minute": 600, "calls_per_hour": 10000},
}

FREE_TIER_MONTHLY_SEARCH_LIMIT = 50

_rate_window: dict[str, list[float]] = {}
_anon_rate_window: dict[str, list[float]] = {}
_ANON_RPM = 15

# ── Redis client (lazy singleton, falls back to in-memory if REDIS_URL unset) ─

_redis_client = None
_redis_init_done = False


def _get_redis():
    global _redis_client, _redis_init_done
    if _redis_init_done:
        return _redis_client
    _redis_init_done = True
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return None
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        logger.info("Redis rate limiter initialised: %s…", url[:30])
    except Exception as exc:
        logger.warning("Redis init failed, using in-memory rate limiter: %s", exc)
        _redis_client = None
    return _redis_client


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _redis_rate_check(redis, key_id: str, limits: dict, tier: str) -> None:
    """Sliding-window rate check via Redis sorted sets. Atomic check-before-record."""
    now = time.time()
    member = str(_uuid_mod.uuid4())
    minute_key = f"wf:rl:m:{key_id}"
    hour_key   = f"wf:rl:h:{key_id}"

    # Prune + count (before recording this request)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zremrangebyscore(minute_key, 0, now - 60)
        pipe.zremrangebyscore(hour_key,   0, now - 3600)
        pipe.zcard(minute_key)
        pipe.zcard(hour_key)
        results = await pipe.execute()
    minute_count, hour_count = results[2], results[3]

    if minute_count >= limits["calls_per_minute"]:
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": limits["calls_per_minute"],
            "window": "per_minute",
            "tier": tier,
            "message": f"Rate limit: {limits['calls_per_minute']} calls/min for {tier} tier.",
            "upgrade_url": "https://wayforth.io/pricing",
        })
    if hour_count >= limits["calls_per_hour"]:
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": limits["calls_per_hour"],
            "window": "per_hour",
            "tier": tier,
            "message": f"Rate limit: {limits['calls_per_hour']} calls/hour for {tier} tier.",
            "upgrade_url": "https://wayforth.io/pricing",
        })

    # Record this request
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zadd(minute_key, {member: now})
        pipe.expire(minute_key, 61)
        pipe.zadd(hour_key,   {member: now})
        pipe.expire(hour_key,   3601)
        await pipe.execute()


def _memory_rate_check(key_id: str, limits: dict, tier: str) -> None:
    """In-process sliding-window rate check (single-replica fallback)."""
    now = time.time()
    window = _rate_window.setdefault(key_id, [])
    window[:] = [t for t in window if t > now - 3600]

    calls_last_minute = sum(1 for t in window if t > now - 60)
    calls_last_hour   = len(window)

    if calls_last_minute >= limits["calls_per_minute"]:
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": limits["calls_per_minute"],
            "window": "per_minute",
            "tier": tier,
            "message": f"Rate limit: {limits['calls_per_minute']} calls/min for {tier} tier.",
            "upgrade_url": "https://wayforth.io/pricing",
        })
    if calls_last_hour >= limits["calls_per_hour"]:
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": limits["calls_per_hour"],
            "window": "per_hour",
            "tier": tier,
            "message": f"Rate limit: {limits['calls_per_hour']} calls/hour for {tier} tier.",
            "upgrade_url": "https://wayforth.io/pricing",
        })
    window.append(now)


def require_tier(tier: str, feature: str) -> None:
    """Raise 403 if tier is not in the allowed list for feature."""
    allowed = TIER_FEATURES.get(feature, [])
    if tier not in allowed:
        min_tier = allowed[0] if allowed else "growth"
        raise HTTPException(status_code=403, detail={
            "error": "tier_required",
            "feature": feature,
            "your_tier": tier,
            "required_tier": min_tier,
            "message": f"This feature requires {min_tier} tier or above.",
            "upgrade_url": "https://wayforth.io/pricing",
        })


async def check_rate_limit(api_key_id: str, tier: str) -> None:
    """Sliding-window rate limiter per api_key_id. Redis-backed; falls back to in-memory."""
    if tier in ("growth", "enterprise"):
        return
    limits = TIER_RATE_LIMITS.get(tier, TIER_RATE_LIMITS["free"])
    redis = _get_redis()
    if redis is not None:
        try:
            await _redis_rate_check(redis, api_key_id, limits, tier)
            return
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Redis rate check failed, falling back to in-memory: %s", exc)
    _memory_rate_check(api_key_id, limits, tier)


async def check_anon_rate_limit(ip: str) -> None:
    """Sliding-window 30 req/min for anonymous /search callers. Redis or in-memory."""
    redis = _get_redis()
    if redis is not None:
        try:
            now = time.time()
            member = str(_uuid_mod.uuid4())
            anon_key = f"wf:rl:anon:{ip}"
            async with redis.pipeline(transaction=False) as pipe:
                pipe.zremrangebyscore(anon_key, 0, now - 60)
                pipe.zcard(anon_key)
                results = await pipe.execute()
            if results[1] >= _ANON_RPM:
                raise HTTPException(status_code=429, detail={
                    "error": "rate_limit_exceeded",
                    "limit": _ANON_RPM,
                    "window": "per_minute",
                    "message": f"Anonymous search limit: {_ANON_RPM} requests/minute. Add an API key for higher limits.",
                    "get_key_url": "https://wayforth.io/dashboard",
                })
            async with redis.pipeline(transaction=False) as pipe:
                pipe.zadd(anon_key, {member: now})
                pipe.expire(anon_key, 61)
                await pipe.execute()
            return
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Redis anon rate check failed, falling back to in-memory: %s", exc)

    now = time.time()
    window = _anon_rate_window.setdefault(ip, [])
    window[:] = [t for t in window if t > now - 60]
    if len(window) >= _ANON_RPM:
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": _ANON_RPM,
            "window": "per_minute",
            "message": f"Anonymous search limit: {_ANON_RPM} requests/minute. Add an API key for higher limits.",
            "get_key_url": "https://wayforth.io/dashboard",
        })
    window.append(now)
