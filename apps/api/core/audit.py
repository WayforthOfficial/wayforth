"""core/audit.py — append-only admin audit logging.

v0.8.0 Item 4. Application logs (v0.7.8) are mutable and rotated; this module
writes a parallel row to admin_audit_log which is append-only enforced at the
DB-trigger level. Callers should keep their existing logger.warning() calls
for observability — the audit row is the system of record.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("wayforth")


async def log_admin_action(
    db,
    admin_session: dict,
    action: str,
    *,
    target_user_id: str | None = None,
    target_resource: str | None = None,
    payload: dict[str, Any] | None = None,
    request=None,
) -> None:
    """Insert one row into admin_audit_log. Errors are logged but never raised
    so an audit-write failure cannot block the underlying admin action — the
    application log still captures it via the caller's existing logger.

    admin_session: the dict returned by routers.admin.dashboard.get_admin_session,
        which contains 'admin_user_id' and 'email'.
    action: snake_case verb, e.g. 'tier_change', 'admin_login', 'user_suspend'.
    target_user_id: the customer affected (UUID string), or None for actions
        without a specific user target.
    target_resource: free-form identifier of the affected resource
        (e.g. service slug, api_key id).
    payload: structured detail to round out the row, JSON-serialised on write.
    request: FastAPI Request, used to capture IP and user-agent.
    """
    admin_id = admin_session.get("admin_user_id")
    admin_email = admin_session.get("email") or ""
    if not admin_id:
        # Defensive: a session without admin_user_id should never reach a
        # mutating endpoint, but if it does we'd rather log loudly than
        # silently drop the audit row.
        logger.error(
            "audit.log_admin_action: missing admin_user_id action=%s email=%s",
            action, admin_email,
        )
        return

    ip_address = None
    user_agent = None
    if request is not None:
        try:
            ip_address = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent")
        except Exception:
            pass

    try:
        await db.execute(
            """
            INSERT INTO admin_audit_log
                (admin_id, admin_email, action, target_user_id, target_resource,
                 payload, ip_address, user_agent)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            admin_id,
            admin_email,
            action,
            target_user_id,
            target_resource,
            json.dumps(payload) if payload else None,
            ip_address,
            user_agent,
        )
    except Exception as e:
        logger.error(
            "audit row write failed action=%s admin=%s target_user=%s: %s",
            action, admin_email, target_user_id, e,
        )
