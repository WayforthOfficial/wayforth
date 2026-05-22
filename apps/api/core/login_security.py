"""Brute-force protection for password-based login endpoints.

Per-email AND per-IP throttling: the per-email counter slows targeted password
guesses against a single account; the per-IP counter slows credential stuffing
that spreads guesses across many accounts. Either trigger produces a 429.
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import HTTPException

logger = logging.getLogger("wayforth.security")

# Redis key prefixes
_KEY_PREFIX = "login_fail:"
_IP_PREFIX = "login_fail_ip:"
# Counters expire after 1 hour regardless of threshold hit
_TTL = 3600

_SOFT_THRESHOLD = 5   # 5-minute lockout
_HARD_THRESHOLD = 10  # 1-hour lockout
# IP thresholds are looser than per-email — shared IPs (mobile networks,
# offices) legitimately produce a handful of failures across users.
_IP_SOFT_THRESHOLD = 20
_IP_HARD_THRESHOLD = 60


def _fail_key(email: str) -> str:
    return _KEY_PREFIX + hashlib.sha256(email.lower().encode()).hexdigest()


def _ip_key(ip: str) -> str:
    return _IP_PREFIX + hashlib.sha256(ip.encode()).hexdigest()


async def check_login_lockout(email: str, redis, ip: str | None = None) -> None:
    """Raise 429 with Retry-After if the email OR ip is currently locked out."""
    if redis is None:
        return
    try:
        raw = await redis.get(_fail_key(email))
        count = int(raw) if raw else 0
        if count >= _HARD_THRESHOLD:
            logger.warning("login lockout (hard, email) hash=%s count=%d", _fail_key(email)[-8:], count)
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts — try again in 1 hour",
                headers={"Retry-After": "3600"},
            )
        if count >= _SOFT_THRESHOLD:
            logger.warning("login lockout (soft, email) hash=%s count=%d", _fail_key(email)[-8:], count)
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts — try again in 5 minutes",
                headers={"Retry-After": "300"},
            )
        if ip:
            ip_raw = await redis.get(_ip_key(ip))
            ip_count = int(ip_raw) if ip_raw else 0
            if ip_count >= _IP_HARD_THRESHOLD:
                logger.warning("login lockout (hard, ip) hash=%s count=%d", _ip_key(ip)[-8:], ip_count)
                raise HTTPException(
                    status_code=429,
                    detail="Too many failed login attempts from this network — try again in 1 hour",
                    headers={"Retry-After": "3600"},
                )
            if ip_count >= _IP_SOFT_THRESHOLD:
                logger.warning("login lockout (soft, ip) hash=%s count=%d", _ip_key(ip)[-8:], ip_count)
                raise HTTPException(
                    status_code=429,
                    detail="Too many failed login attempts from this network — try again in 5 minutes",
                    headers={"Retry-After": "300"},
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug("login lockout check failed (ignoring): %s", exc)


async def record_login_failure(email: str, redis, ip: str | None = None) -> None:
    """Increment the email failure counter AND, if provided, the IP counter.
    First write of each sets TTL to 1 hour."""
    if redis is None:
        return
    try:
        key = _fail_key(email)
        pipe = redis.pipeline()
        await pipe.incr(key)
        await pipe.expire(key, _TTL)
        if ip:
            ipk = _ip_key(ip)
            await pipe.incr(ipk)
            await pipe.expire(ipk, _TTL)
        results = await pipe.execute()
        count = results[0]
        logger.warning("login failure recorded email_hash=%s count=%d", key[-8:], count)
    except Exception as exc:
        logger.debug("record_login_failure failed (ignoring): %s", exc)


async def clear_login_failures(email: str, redis) -> None:
    """Reset the per-email counter after a successful login.
    Per-IP counter is intentionally NOT cleared — a successful login from a
    bursting IP shouldn't reset its rate limit immediately."""
    if redis is None:
        return
    try:
        await redis.delete(_fail_key(email))
    except Exception as exc:
        logger.debug("clear_login_failures failed (ignoring): %s", exc)
