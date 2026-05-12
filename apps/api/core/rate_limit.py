import hashlib
import os
from datetime import datetime, timezone

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address  # fallback only

# Number of *trusted* proxy hops in front of the app. Railway adds 1.
# The rightmost N entries of X-Forwarded-For were appended by trusted hops,
# so the client IP is the entry immediately to the left of those. A client
# can only forge entries to the *left* of the trusted segment.
_TRUSTED_PROXY_HOPS = int(os.environ.get("WAYFORTH_TRUSTED_PROXY_HOPS", "1"))


def get_real_ip(request: Request) -> str:
    """Return the client IP recorded by the closest trusted proxy.

    Each trusted hop in the proxy chain appends one entry to XFF (the source it
    saw). With N trusted hops, the real client IP is `parts[-N]` — anything
    further left was supplied by the client and is untrusted. If XFF has fewer
    entries than N, the trusted chain didn't append normally and we fall back
    to the direct connection address.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded and _TRUSTED_PROXY_HOPS > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= _TRUSTED_PROXY_HOPS:
            return parts[-_TRUSTED_PROXY_HOPS]
    return get_remote_address(request)


def rate_limit_key(request: Request) -> str:
    """Slowapi key function. Keys authenticated requests on the API key hash
    so a client cannot bypass per-endpoint rate limits by rotating XFF.
    Anonymous requests fall back to the trusted IP."""
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        return "k:" + hashlib.sha256(raw_key.encode()).hexdigest()[:16]
    return "ip:" + get_real_ip(request)


limiter = Limiter(key_func=rate_limit_key)

_X402_RPM = {
    "unknown":     10,
    "emerging":    30,
    "established": 60,
    "trusted":     120,
    "elite":       None,   # unlimited
}

# wallet_address → {count, window_start}
_x402_rate_state: dict = {}


def _check_x402_rate_limit(wallet: str, tier: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Thread-safe for single-process deployment."""
    import time as _t
    limit = _X402_RPM.get(tier)
    if limit is None:
        return True, 0
    now = _t.time()
    state = _x402_rate_state.get(wallet)
    if state is None or now - state["window_start"] >= 60:
        _x402_rate_state[wallet] = {"count": 1, "window_start": now}
        return True, 0
    if state["count"] >= limit:
        retry_after = max(1, int(60 - (now - state["window_start"])))
        return False, retry_after
    state["count"] += 1
    return True, 0
