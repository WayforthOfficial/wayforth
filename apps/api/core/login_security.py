"""Brute-force protection for password-based login endpoints.

Per-email AND per-IP throttling: the per-email counter slows targeted password
guesses against a single account; the per-IP counter slows credential stuffing
that spreads guesses across many accounts. Either trigger produces a 429.
"""
from __future__ import annotations

import hashlib
import logging
import time

from fastapi import HTTPException

logger = logging.getLogger("wayforth.security")

# FINDING-012: fail-closed fallback when Redis is unavailable.
# When the Redis-backed counters can't be read/written, we do NOT silently skip
# throttling. Instead we use a small in-process counter that locks much harder
# (3 attempts → 5-minute lock) so a Redis outage can't open a brute-force window.
# Bounded to cap memory; oldest entries evicted when full.
_FALLBACK_MAX_ATTEMPTS = 3
_FALLBACK_LOCK_SECONDS = 300
_FALLBACK_CAP = 50_000
_fallback_counts: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)


def _fallback_register_and_check(key: str) -> bool:
    """Increment the in-memory counter for `key`; return True if it is locked."""
    now = time.time()
    if len(_fallback_counts) > _FALLBACK_CAP:
        for k in sorted(_fallback_counts, key=lambda k: _fallback_counts[k][1])[:1000]:
            _fallback_counts.pop(k, None)
    count, window_start = _fallback_counts.get(key, (0, now))
    if now - window_start >= _FALLBACK_LOCK_SECONDS:
        count, window_start = 0, now
    count += 1
    _fallback_counts[key] = (count, window_start)
    return count > _FALLBACK_MAX_ATTEMPTS

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


def _fallback_lockout(email: str, ip: str | None) -> None:
    """Fail-closed path used when Redis is unavailable. Logs CRITICAL (Redis loss
    during auth is itself a security event) and applies the strict in-memory cap."""
    logger.critical(
        "AUTH THROTTLE DEGRADED: Redis unavailable during login lockout check — "
        "applying strict in-memory fallback (3 attempts / 5 min)."
    )
    locked = _fallback_register_and_check(_fail_key(email))
    if ip:
        locked = _fallback_register_and_check(_ip_key(ip)) or locked
    if locked:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts — try again in 5 minutes",
            headers={"Retry-After": "300"},
        )


async def check_login_lockout(email: str, redis, ip: str | None = None) -> None:
    """Raise 429 with Retry-After if the email OR ip is currently locked out.

    FINDING-012: if Redis is unavailable we fail CLOSED via _fallback_lockout
    rather than silently returning (which previously removed brute-force
    protection entirely whenever Redis was degraded)."""
    if redis is None:
        _fallback_lockout(email, ip)
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
        # FINDING-012: a Redis error mid-check must not open a brute-force window.
        logger.error("login lockout check failed — failing closed: %s", exc)
        _fallback_lockout(email, ip)


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


# ── Admin gate: stricter per-IP throttle (FRONTEND B-001) ─────────────────────
# The admin login is a higher-value target than user login, so it gets a much
# tighter per-IP gate than the shared _IP_*_THRESHOLD values above: 3 failed
# attempts from one IP within 1 hour → hard 3-hour lock.
_ADMIN_IP_FAIL_PREFIX = "admin_login_fail_ip:"
_ADMIN_IP_LOCK_PREFIX = "admin_login_lock_ip:"
_ADMIN_IP_MAX_FAILS = 3
_ADMIN_IP_WINDOW = 3600        # count failures within a 1-hour rolling window
_ADMIN_IP_LOCK_SECONDS = 10800  # 3-hour lock once the threshold is hit


def _admin_ip_fail_key(ip: str) -> str:
    return _ADMIN_IP_FAIL_PREFIX + hashlib.sha256(ip.encode()).hexdigest()


def _admin_ip_lock_key(ip: str) -> str:
    return _ADMIN_IP_LOCK_PREFIX + hashlib.sha256(ip.encode()).hexdigest()


async def check_admin_login_lockout(redis, ip: str | None) -> None:
    """Raise 429 if `ip` is currently locked out of the admin login.

    Fails CLOSED if Redis is unavailable (reuses the strict in-memory fallback),
    so a Redis outage can't open an admin brute-force window."""
    if not ip:
        return
    if redis is None:
        if _fallback_register_and_check(_admin_ip_lock_key(ip)):
            raise HTTPException(
                status_code=429,
                detail="Too many failed admin login attempts — try again later",
                headers={"Retry-After": str(_FALLBACK_LOCK_SECONDS)},
            )
        return
    try:
        locked = await redis.get(_admin_ip_lock_key(ip))
        if locked:
            ttl = await redis.ttl(_admin_ip_lock_key(ip))
            retry = ttl if (ttl and ttl > 0) else _ADMIN_IP_LOCK_SECONDS
            logger.warning("admin login lockout (ip) hash=%s ttl=%s", _admin_ip_lock_key(ip)[-8:], retry)
            raise HTTPException(
                status_code=429,
                detail="Too many failed admin login attempts — locked for 3 hours",
                headers={"Retry-After": str(retry)},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("admin login lockout check failed — failing closed: %s", exc)
        if _fallback_register_and_check(_admin_ip_lock_key(ip)):
            raise HTTPException(
                status_code=429,
                detail="Too many failed admin login attempts — try again later",
                headers={"Retry-After": str(_FALLBACK_LOCK_SECONDS)},
            )


async def record_admin_login_failure(redis, ip: str | None) -> None:
    """Count an admin login failure for `ip`; set the 3-hour lock at the threshold."""
    if redis is None or not ip:
        return
    try:
        key = _admin_ip_fail_key(ip)
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _ADMIN_IP_WINDOW)
        if count >= _ADMIN_IP_MAX_FAILS:
            await redis.set(_admin_ip_lock_key(ip), "1", ex=_ADMIN_IP_LOCK_SECONDS)
            logger.warning("admin login IP locked for 3h (>=%d fails) hash=%s", _ADMIN_IP_MAX_FAILS, key[-8:])
    except Exception as exc:
        logger.debug("record_admin_login_failure failed (ignoring): %s", exc)


async def clear_admin_login_failures(redis, ip: str | None) -> None:
    """Clear the admin per-IP failure counter after a successful admin login.
    The lock key is left untouched (a locked IP can't reach success anyway)."""
    if redis is None or not ip:
        return
    try:
        await redis.delete(_admin_ip_fail_key(ip))
    except Exception as exc:
        logger.debug("clear_admin_login_failures failed (ignoring): %s", exc)
