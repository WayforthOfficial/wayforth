"""tier_gates.py — Feature availability and per-tier rate limits."""
import time
from fastapi import HTTPException

TIER_FEATURES: dict[str, list[str]] = {
    "search":             ["free", "builder", "starter", "pro", "growth"],
    "execute_managed":    ["free", "builder", "starter", "pro", "growth"],
    "run":                ["free", "builder", "starter", "pro", "growth"],
    "leaderboard":        ["free", "builder", "starter", "pro", "growth"],
    "status":             ["free", "builder", "starter", "pro", "growth"],
    "account_balance":    ["free", "builder", "starter", "pro", "growth"],
    "byok":               ["builder", "starter", "pro", "growth"],
    "webhooks":           ["builder", "starter", "pro", "growth"],
    "agent_id_tagging":   ["builder", "starter", "pro", "growth"],
    "account_agents":     ["builder", "starter", "pro", "growth"],
    "wri_alerts":         ["builder", "starter", "pro", "growth"],
    "topup_usdc":         ["builder", "starter", "pro", "growth"],
    "compare":            ["starter", "pro", "growth"],
    "analytics":          ["starter", "pro", "growth"],
    "wayforthql":         ["starter", "pro", "growth"],
    "wri_scores_visible": ["pro", "growth"],
    "priority_execution": ["pro", "growth"],
    "agent_identity":     ["pro", "growth"],
    "custom_services":    ["growth"],
    "no_rate_limits":     ["growth"],
}

TIER_RATE_LIMITS: dict[str, dict] = {
    "free":    {"calls_per_minute": 15,  "calls_per_hour": 100},
    "builder": {"calls_per_minute": 120, "calls_per_hour": 500},
    "starter": {"calls_per_minute": 300, "calls_per_hour": 1500},
    "pro":     {"calls_per_minute": 600, "calls_per_hour": 5000},
    "growth":  {"calls_per_minute": 600, "calls_per_hour": 10000},
}

FREE_TIER_MONTHLY_SEARCH_LIMIT = 50

_rate_window: dict[str, list[float]] = {}
_anon_rate_window: dict[str, list[float]] = {}
_ANON_RPM = 15


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


def check_rate_limit(api_key_id: str, tier: str) -> None:
    """Sliding-window rate limiter per api_key_id. Raises 429 if over limit."""
    if tier == "growth":
        return
    limits = TIER_RATE_LIMITS.get(tier, TIER_RATE_LIMITS["free"])
    now = time.time()
    window = _rate_window.setdefault(api_key_id, [])
    window[:] = [t for t in window if t > now - 3600]

    calls_last_minute = sum(1 for t in window if t > now - 60)
    calls_last_hour = len(window)

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


def check_anon_rate_limit(ip: str) -> None:
    """Sliding-window 30 req/min rate limit for unauthenticated /search callers."""
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
