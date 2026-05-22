"""Brute-force protection for password-based login endpoints."""
from __future__ import annotations

import hashlib
import logging

from fastapi import HTTPException

logger = logging.getLogger("wayforth.security")

# Redis key prefix; value is failure count (string int)
_KEY_PREFIX = "login_fail:"
# Counters expire after 1 hour regardless of threshold hit
_TTL = 3600

_SOFT_THRESHOLD = 5   # 5-minute lockout
_HARD_THRESHOLD = 10  # 1-hour lockout


def _fail_key(email: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(email.lower().encode()).hexdigest()


async def check_login_lockout(email: str, redis) -> None:
    """Raise 429 with Retry-After if the email is currently locked out."""
    if redis is None:
        return
    try:
        raw = await redis.get(_fail_key(email))
        count = int(raw) if raw else 0
        if count >= _HARD_THRESHOLD:
            logger.warning("login lockout (hard) email_hash=%s count=%d", _fail_key(email)[-8:], count)
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts — try again in 1 hour",
                headers={"Retry-After": "3600"},
            )
        if count >= _SOFT_THRESHOLD:
            logger.warning("login lockout (soft) email_hash=%s count=%d", _fail_key(email)[-8:], count)
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts — try again in 5 minutes",
                headers={"Retry-After": "300"},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("login lockout check failed (ignoring): %s", exc)


async def record_login_failure(email: str, redis) -> None:
    """Increment the failure counter. First write sets TTL to 1 hour."""
    if redis is None:
        return
    try:
        key = _fail_key(email)
        pipe = redis.pipeline()
        await pipe.incr(key)
        await pipe.expire(key, _TTL)
        results = await pipe.execute()
        count = results[0]
        logger.warning("login failure recorded email_hash=%s count=%d", key[-8:], count)
    except Exception as exc:
        logger.debug("record_login_failure failed (ignoring): %s", exc)


async def clear_login_failures(email: str, redis) -> None:
    """Reset the counter after a successful login."""
    if redis is None:
        return
    try:
        await redis.delete(_fail_key(email))
    except Exception as exc:
        logger.debug("clear_login_failures failed (ignoring): %s", exc)
