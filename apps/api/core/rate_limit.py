from datetime import datetime, timezone

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address  # fallback only


def get_real_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)

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
