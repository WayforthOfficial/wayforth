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
    "compare":            ["builder", "pro", "growth", "enterprise"],
    "analytics":          ["builder", "pro", "growth", "enterprise"],
    "wayforthql":         ["builder", "pro", "growth", "enterprise"],
    "wri_scores_visible": ["pro", "growth", "enterprise"],
    "priority_execution": ["pro", "growth", "enterprise"],
    "agent_identity":     ["pro", "growth", "enterprise"],
    "custom_services":    ["growth", "enterprise"],
    "no_rate_limits":     ["growth", "enterprise"],
    "cloud_agents":       ["free", "builder", "starter", "pro", "growth", "enterprise"],
    "priority_support":   ["enterprise"],
}

# Max hosted agents a user may have deployed simultaneously, by tier.
HOSTED_AGENT_LIMITS: dict[str, int] = {
    "free":       1,
    "starter":    3,
    "builder":    5,
    "pro":        8,
    "growth":     10,
    "enterprise": 50,
}

# Max simultaneous queued+running cloud agent runs per user, by tier.
CONCURRENT_RUNS_PER_USER: dict[str, int] = {
    "free":       1,
    "starter":    1,
    "builder":    2,
    "pro":        5,
    "growth":     10,
    "enterprise": 25,
}

TIER_RATE_LIMITS: dict[str, dict] = {
    "free":       {"calls_per_minute": 15,  "calls_per_hour": 100},
    "starter":    {"calls_per_minute": 120, "calls_per_hour": 500},
    "builder":    {"calls_per_minute": 300, "calls_per_hour": 1500},
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
        logger.info("rate limiter: in-memory (no REDIS_URL)")
        return None
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        logger.info("rate limiter: redis (connected)")
    except Exception as exc:
        logger.info("rate limiter: in-memory (redis failed)")
        logger.debug("Redis init error: %s", exc)
        _redis_client = None
    return _redis_client


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _redis_rate_check(redis, key_id: str, limits: dict, tier: str) -> None:
    """Sliding-window rate check via Redis sorted sets.

    Strategy: prune → add this request → count → check. Doing the add BEFORE
    the count makes the check race-tight: under N concurrent requests, every
    task sees a count that includes its own contribution, so the only way
    `count <= limit` is if at most `limit` tasks have added so far. Tasks
    over the limit roll back their add and 429.

    The previous form (prune → count → check → add, with an `await` between
    count and add) had a TOCTOU window where N concurrent tasks could all
    read count=0 before any wrote, allowing well over `limit` requests to
    slip through under burst.
    """
    now = time.time()
    member = str(_uuid_mod.uuid4())
    minute_key = f"wf:rl:m:{key_id}"
    hour_key   = f"wf:rl:h:{key_id}"

    # Prune old + add this request + count + expire — all in one pipeline.
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zremrangebyscore(minute_key, 0, now - 60)
        pipe.zremrangebyscore(hour_key,   0, now - 3600)
        pipe.zadd(minute_key, {member: now})
        pipe.zadd(hour_key,   {member: now})
        pipe.zcard(minute_key)
        pipe.zcard(hour_key)
        pipe.expire(minute_key, 61)
        pipe.expire(hour_key, 3601)
        results = await pipe.execute()
    minute_count, hour_count = results[4], results[5]

    if minute_count > limits["calls_per_minute"]:
        # Roll back so we don't permanently inflate this client's window.
        try:
            await redis.zrem(minute_key, member)
            await redis.zrem(hour_key, member)
        except Exception:
            pass  # non-critical: rollback best-effort; 429 is still raised regardless
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": limits["calls_per_minute"],
            "window": "per_minute",
            "tier": tier,
            "message": f"Rate limit: {limits['calls_per_minute']} calls/min for {tier} tier.",
            "upgrade_url": "https://wayforth.io/pricing",
        })
    if hour_count > limits["calls_per_hour"]:
        try:
            await redis.zrem(minute_key, member)
            await redis.zrem(hour_key, member)
        except Exception:
            pass  # non-critical: rollback best-effort; 429 is still raised regardless
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": limits["calls_per_hour"],
            "window": "per_hour",
            "tier": tier,
            "message": f"Rate limit: {limits['calls_per_hour']} calls/hour for {tier} tier.",
            "upgrade_url": "https://wayforth.io/pricing",
        })


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
    """Per-minute sliding-window limit for unauthenticated /search callers.

    Limit: `_ANON_RPM` (currently 15) requests per IP per 60-second window.
    Layered behind the stricter per-IP daily wall in `core.auth.check_auth`
    (`_ANON_DAILY_LIMIT = 3`): under normal operation the daily wall fires
    first, but the per-minute limit guards against bursts that arrive
    inside a 24-hour window where the daily counter hasn't yet incremented
    (e.g. just after midnight UTC) or against any future relaxation of the
    daily cap.

    Same race-tight strategy as `_redis_rate_check`: add-then-count-then-
    rollback rather than count-then-add, so concurrent requests cannot all
    read count=0 before any of them increments.
    """
    redis = _get_redis()
    if redis is not None:
        try:
            now = time.time()
            member = str(_uuid_mod.uuid4())
            anon_key = f"wf:rl:anon:{ip}"
            async with redis.pipeline(transaction=False) as pipe:
                pipe.zremrangebyscore(anon_key, 0, now - 60)
                pipe.zadd(anon_key, {member: now})
                pipe.zcard(anon_key)
                pipe.expire(anon_key, 61)
                results = await pipe.execute()
            count = results[2]
            if count > _ANON_RPM:
                try:
                    await redis.zrem(anon_key, member)
                except Exception:
                    pass  # non-critical: rollback best-effort; 429 is still raised regardless
                raise HTTPException(status_code=429, detail={
                    "error": "rate_limit_exceeded",
                    "limit": _ANON_RPM,
                    "window": "per_minute",
                    "message": f"Anonymous search limit: {_ANON_RPM} requests/minute. Add an API key for higher limits.",
                    "get_key_url": "https://wayforth.io/dashboard",
                })
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
