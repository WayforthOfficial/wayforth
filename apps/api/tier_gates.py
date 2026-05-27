"""tier_gates.py — Feature availability and per-tier rate limits."""
import logging
import time
from collections import deque
from fastapi import HTTPException

logger = logging.getLogger("wayforth.tier_gates")

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
    "free":    {"calls_per_minute": 30,  "calls_per_hour": 100},
    "builder": {"calls_per_minute": 120, "calls_per_hour": 500},
    "starter": {"calls_per_minute": 300, "calls_per_hour": 1500},
    "pro":     {"calls_per_minute": 600, "calls_per_hour": 5000},
    "growth":  {"calls_per_minute": 600, "calls_per_hour": 10000},
}

FREE_TIER_MONTHLY_SEARCH_LIMIT = 50

# S9 (v0.7.8): cap per-key window at the highest legitimate hourly rate (pro =
# 5000) plus headroom. Growth tier is exempted before insert, so 10000 is a
# hard upper bound. Without maxlen, a single key making 1 req/sec accumulated
# 86400 entries/day → unbounded RAM under steady load.
_RATE_WINDOW_MAXLEN = 10000
_rate_window: dict[str, deque] = {}


def require_tier(tier: str, feature: str) -> None:
    """Raise 403 if tier is not in the allowed list for feature."""
    allowed = TIER_FEATURES.get(feature, [])
    if tier not in allowed:
        min_tier = allowed[0] if allowed else "growth"
        # L6 (v0.7.8): log every tier-gate denial. Caller chain has the user
        # identity already; this gives ops the feature + tier + minimum so
        # they can correlate with surface-probing attempts.
        logger.info(
            "tier_gate_denied feature=%s user_tier=%s required_tier=%s",
            feature, tier, min_tier,
        )
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
    window = _rate_window.setdefault(api_key_id, deque(maxlen=_RATE_WINDOW_MAXLEN))
    # Time-prune entries older than 1h from the left (oldest first).
    while window and window[0] <= now - 3600:
        window.popleft()

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


_anon_ip_window: dict[str, deque] = {}
_ANON_SEARCH_RPM = 30


def check_anon_rate_limit(ip: str) -> None:
    """Sliding-window rate limiter for unauthenticated /search requests: 30 req/min per IP."""
    now = time.time()
    # S9 (v0.7.8): per-IP deque capped at the per-minute limit. Anonymous
    # users have a 60-second window so memory is naturally tight, but the
    # explicit maxlen prevents the dict-of-lists footgun if the limit grows.
    window = _anon_ip_window.setdefault(ip, deque(maxlen=_ANON_SEARCH_RPM))
    while window and window[0] <= now - 60:
        window.popleft()
    if len(window) >= _ANON_SEARCH_RPM:
        raise HTTPException(status_code=429, detail={
            "error": "rate_limit_exceeded",
            "limit": _ANON_SEARCH_RPM,
            "window": "per_minute",
            "tier": "anonymous",
            "message": f"Rate limit: {_ANON_SEARCH_RPM} calls/min for unauthenticated requests.",
            "signup_url": "https://wayforth.io/signup",
        })
    window.append(now)
