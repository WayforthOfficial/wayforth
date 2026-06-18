"""core/admin_auth.py — single source of truth for admin authentication.

AUTHZ-1 (2026-06): the static X-Admin-Key is a *break-glass* mechanism. It must be
gated on WAYFORTH_ADMIN_KEY_ENABLED (default off) so that a leaked key is inert in
a hardened production deploy, and every use must be audit-logged. Previously only
`get_admin_session` (dashboard.py) enforced this gate; `_admin_ok` and a dozen
inline `secrets.compare_digest(provided, ADMIN_KEY)` checks across the admin
routers compared the key directly, bypassing the kill switch entirely — a leaked
key still reached destructive purge + credit-minting USDC reconcile etc. even with
the switch off.

Every static-key check now routes through `admin_key_ok()`, and the combined
`admin_authed()` (session token OR gated break-glass key) backs the boolean-style
checks so that turning the break-glass switch OFF does not lock admins out of the
key-only endpoints — they remain reachable via a normal X-Admin-Token session.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets

from fastapi import Request

logger = logging.getLogger("wayforth")


def admin_key_ok(request: Request) -> bool:
    """True iff a valid X-Admin-Key is present AND break-glass is enabled.

    This is the ONLY place a static ADMIN_KEY may be compared. It enforces the
    WAYFORTH_ADMIN_KEY_ENABLED env gate (default 'false') and audit-logs both a
    blocked attempt and a successful break-glass use.
    """
    from main import ADMIN_KEY
    provided = request.headers.get("X-Admin-Key", "")
    if not provided or not ADMIN_KEY:
        return False
    enabled = os.environ.get("WAYFORTH_ADMIN_KEY_ENABLED", "false").lower() == "true"
    if not enabled:
        logger.warning(
            "X-Admin-Key presented but disabled by WAYFORTH_ADMIN_KEY_ENABLED=false path=%s",
            request.url.path,
        )
        return False
    if secrets.compare_digest(provided, ADMIN_KEY):
        logger.warning(
            "ADMIN_KEY break-glass used env=%s ip=%s ua=%s path=%s",
            os.environ.get("ENVIRONMENT", "development"),
            request.client.host if request.client else "?",
            request.headers.get("user-agent", "?")[:80],
            request.url.path,
        )
        return True
    return False


async def admin_token_ok(request: Request, db) -> bool:
    """True iff a valid, active, unexpired X-Admin-Token session is presented."""
    token = request.headers.get("X-Admin-Token", "")
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = await db.fetchrow(
        "SELECT s.expires_at, u.is_active FROM admin_sessions s "
        "JOIN admin_users u ON u.id = s.admin_user_id "
        "WHERE s.token_hash = $1 AND s.expires_at > NOW()",
        token_hash,
    )
    return bool(row and row["is_active"])


async def admin_authed(request: Request, db) -> bool:
    """True for an authenticated admin: a valid session token OR the gated key."""
    if admin_key_ok(request):
        return True
    return await admin_token_ok(request, db)
