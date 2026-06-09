"""core/session.py — Redis-backed browser session store.

Eliminates the localStorage-stored Supabase JWT pattern for the dashboard.
The dashboard exchanges its short-lived Supabase JWT for an opaque server-issued
session token at POST /auth/session; the token is delivered ONLY as an
HttpOnly/Secure/SameSite=Strict cookie. JavaScript cannot read HttpOnly cookies,
so an XSS on any wayforth.io subdomain can no longer steal the session.

Design choices:
  - Opaque random token (`secrets.token_urlsafe(48)` → 384 bits), not a JWT.
    Lets us revoke server-side and keeps the cookie value uninformative.
  - Redis key is `session:<sha256(token)>`. The raw cookie value never appears
    in Redis or logs — a Redis dump exposes no active sessions.
  - Cookie sent with HttpOnly + Secure + SameSite=Strict. wayforth.io and
    gateway.wayforth.io share the same registrable domain (wayforth.io) so
    SameSite-Strict still permits the cross-origin fetches the dashboard does.
  - API key flows (X-Wayforth-API-Key header) are unaffected — this module
    only exists for browser sessions that previously rode on Supabase Bearer
    tokens in JS-readable storage.
"""
from __future__ import annotations

import hashlib
import json as _json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import Response
from starlette.requests import Request

logger = logging.getLogger("wayforth.session")

SESSION_COOKIE_NAME = "wf_session"
SESSION_TTL_SECONDS = 3600  # 1 hour idle window, refreshed on /auth/session/refresh
_REDIS_KEY_PREFIX = "session:"
_SCOPE_KEY_RECORD = "wayforth_session"
_SCOPE_KEY_TOKEN = "wayforth_session_token"


def _redis_key(raw_token: str) -> str:
    return _REDIS_KEY_PREFIX + hashlib.sha256(raw_token.encode()).hexdigest()


async def create_session(
    redis,
    user_id: str,
    email: str,
    tier: str | None,
    supabase_id: str,
) -> str:
    """Mint a fresh session, persist its record in Redis, return the raw cookie token.

    The raw token is what gets set as the cookie value; Redis stores only its
    sha256, so a Redis read never yields an active cookie.
    """
    raw_token = secrets.token_urlsafe(48)
    record = {
        "user_id": str(user_id),
        "email": email,
        "tier": tier or "free",
        "supabase_id": supabase_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis.set(_redis_key(raw_token), _json.dumps(record), ex=SESSION_TTL_SECONDS)
    return raw_token


async def get_session(redis, raw_token: str) -> dict | None:
    """Return the session record for `raw_token` or None if absent/expired/malformed."""
    if not raw_token:
        return None
    try:
        raw = await redis.get(_redis_key(raw_token))
    except Exception as exc:
        logger.warning("get_session redis read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        # A malformed record is treated as a miss rather than 500'ing the
        # request — better to force a fresh login than to leak details.
        logger.warning("get_session: malformed record for token hash %s", _redis_key(raw_token)[-8:])
        return None


async def refresh_session(redis, raw_token: str) -> tuple[dict, str] | None:
    """Rotate the session token and bump TTL.

    S17 (v0.7.8): the token value now rotates on every refresh. A stolen
    cookie loses validity at the next refresh tick rather than persisting
    for the full TTL window. We write the new key BEFORE deleting the old
    one — if the new SET fails, the old key is still valid so the user
    is not logged out by a transient Redis hiccup.

    Returns (record, new_raw_token) or None if the session is gone.
    """
    old_key = _redis_key(raw_token)
    try:
        raw = await redis.get(old_key)
    except Exception as exc:
        logger.warning("refresh_session redis read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        record = _json.loads(raw)
    except (ValueError, TypeError):
        return None

    new_token = secrets.token_urlsafe(48)
    new_key = _redis_key(new_token)
    try:
        await redis.set(new_key, raw, ex=SESSION_TTL_SECONDS)
    except Exception as exc:
        logger.warning("refresh_session new-key SET failed: %s", exc)
        return None
    try:
        await redis.delete(old_key)
    except Exception as exc:
        # New token is live; old one will expire naturally in ≤ SESSION_TTL.
        logger.warning("refresh_session old-key DELETE failed: %s", exc)
    return record, new_token


async def revoke_session(redis, raw_token: str) -> None:
    """Idempotent — fine to call on already-expired or non-existent tokens."""
    if not raw_token:
        return
    try:
        await redis.delete(_redis_key(raw_token))
    except Exception as exc:
        logger.warning("revoke_session error: %s", exc)


async def revoke_all_user_sessions(redis, user_id: str) -> int:
    """Revoke every active session for `user_id` (FINDING-106).

    Session records carry their user_id but Redis has no user_id→token index,
    so we SCAN the `session:` keyspace and delete the records that match. Used
    by the account-deletion reaper to ensure no session minted during the grace
    window outlives the purge. Returns the count revoked; best-effort (logs and
    returns on Redis error rather than raising)."""
    if redis is None or not user_id:
        return 0
    revoked = 0
    cursor = 0
    try:
        while True:
            cursor, keys = await redis.scan(
                cursor=cursor, match=_REDIS_KEY_PREFIX + "*", count=200
            )
            for key in keys:
                try:
                    raw = await redis.get(key)
                    if not raw:
                        continue
                    rec = _json.loads(raw)
                    if str(rec.get("user_id")) == str(user_id):
                        await redis.delete(key)
                        revoked += 1
                except Exception:
                    continue  # malformed/unreadable record — skip
            if cursor == 0:
                break
    except Exception as exc:
        logger.warning("revoke_all_user_sessions scan failed user=%s: %s", user_id, exc)
    return revoked


def set_session_cookie(response: Response, raw_token: str) -> None:
    """Set the wf_session cookie with the hardened attribute set.

    HttpOnly — JS `document.cookie` cannot read it (XSS-resistant).
    Secure  — never sent over plain HTTP.
    SameSite=Strict — never sent on cross-site requests.
                       wayforth.io and gateway.wayforth.io share the same
                       registrable domain so the dashboard's cross-origin
                       fetches still carry the cookie.
    Path=/  — applies to every API path.
    Max-Age — matches the Redis TTL so client and server expire together.
    Domain  — NOT set; browser scopes the cookie to the response host
              (gateway.wayforth.io), not the parent zone.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        secure=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the cookie client-side. Pair with `revoke_session` server-side."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=True,
        samesite="strict",
    )


# ── Request helpers ──────────────────────────────────────────────────────────

def get_request_session(request: Request) -> dict | None:
    """Return the validated session record stashed by SessionCookieMiddleware, or None.

    Endpoints that want cookie auth call this; it never raises. If the request
    has no cookie, or the cookie was invalid / expired, the result is None and
    the caller should fall back to its existing auth mechanism (Bearer JWT or
    X-Wayforth-API-Key).
    """
    return request.scope.get(_SCOPE_KEY_RECORD)


def get_request_session_token(request: Request) -> str | None:
    """Return the raw cookie token if the middleware found one, else None."""
    return request.scope.get(_SCOPE_KEY_TOKEN)


def _stash_on_scope(scope: dict, record: dict, raw_token: str) -> None:
    """Used by SessionCookieMiddleware to record the validated session."""
    scope[_SCOPE_KEY_RECORD] = record
    scope[_SCOPE_KEY_TOKEN] = raw_token
