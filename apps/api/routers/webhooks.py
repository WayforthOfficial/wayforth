"""routers/webhooks.py — Webhook registration, listing, deletion, and WRI alerts."""

import asyncio
import hashlib
import hmac
import json as json_lib
import logging
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from pydantic import BaseModel

from core.auth import _resolve_user, resolve_dashboard_caller
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier
from core.url_validation import validate_external_url
from services.managed import SERVICE_DISPLAY_NAMES, SERVICE_CONFIGS

logger = logging.getLogger("wayforth")

router = APIRouter()

_VALID_ALERT_CATEGORIES = {
    "translation", "inference", "search", "image", "audio",
    "finance", "weather", "email", "data",
}


class WebhookRegistration(BaseModel):
    service_id: str | None = None
    url: str = ""
    webhook_url: str = ""
    contact_email: str = ""
    events: list[str] = ["tier_change", "health_alert"]

    @property
    def resolved_url(self) -> str:
        resolved = self.url or self.webhook_url
        if not resolved:
            raise ValueError("url or webhook_url is required")
        return resolved


def _log_safe_url(url: str) -> str:
    """L9 (v0.7.8): strip query string before logging in case the user
    embedded a secret in the URL (`?token=...`). The actual POST still uses
    the full URL — this is purely for log hygiene."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()
    except Exception:
        return "<unparseable>"  # non-critical: URL log hygiene only


async def _enqueue_wri_alert(
    pool, alert, entry, fired_at, fired_at_iso
) -> bool:
    """v0.8.0 Item 5: build payload, validate URL, enqueue into
    webhook_deliveries with kind='wri_alert'. The generic _webhook_retry_loop
    handles HMAC signing + POST + retry. Returns True if enqueued.

    Updates wri_alerts.last_fired_at + fired_count at enqueue time so the
    24h cooldown is honoured immediately (a slow worker should not let the
    same alert be re-enqueued before it fires).
    """
    old_wri = entry.get("old_wri")
    new_wri = entry.get("new_wri")
    total_signals = entry.get("total_signals", 0)
    svc_slug = entry.get("service", "")
    svc_category = entry.get("category") or ""
    pay_rate = entry.get("payment_rate", 0)
    threshold = float(alert["threshold_score"])

    svc_name = SERVICE_DISPLAY_NAMES.get(svc_slug, svc_slug)
    is_managed = svc_slug in SERVICE_CONFIGS
    is_new = old_wri is None

    if is_new:
        msg = (f"A new service ({svc_name}) just entered WayforthRank above your "
               f"threshold of {threshold}. Current score: {round(new_wri, 1)} "
               f"({total_signals} signals, {pay_rate}% payment conversion).")
    else:
        msg = (f"{svc_name} crossed your WRI alert threshold of {threshold}. "
               f"Current score: {round(new_wri, 1)} "
               f"({total_signals} signals, {pay_rate}% payment conversion).")

    payload = {
        "event": "wri.threshold_crossed",
        "service": {
            "slug": svc_slug,
            "name": svc_name,
            "category": svc_category,
            "old_wri": round(old_wri, 2) if old_wri is not None else None,
            "new_wri": round(new_wri, 2),
            "total_signals": total_signals,
            "payment_rate": pay_rate,
            "managed": is_managed,
            "zero_setup": is_managed,
        },
        "alert": {
            "id": str(alert["id"]),
            "threshold_score": threshold,
            "category": alert["category"],
        },
        "fired_at": fired_at_iso,
        "message": msg,
    }
    body = json_lib.dumps(payload)

    if not alert["hmac_secret"]:
        logger.error(
            "wri_alert missing hmac_secret alert=%s — skipping enqueue; recreate the alert",
            alert["id"],
        )
        return False

    try:
        validate_external_url(alert["notify_url"], field_name="notify_url")
    except Exception as _vexc:
        logger.warning("wri_alert refused alert=%s url=%s: %s",
                       alert["id"], _log_safe_url(alert["notify_url"]), _vexc)
        return False

    async with pool.acquire() as db:
        await db.execute("""
            UPDATE wri_alerts
            SET last_fired_at = $1, fired_count = fired_count + 1
            WHERE id = $2
        """, fired_at, alert["id"])
        await db.execute("""
            INSERT INTO webhook_deliveries
                (kind, webhook_id, source_id, event, payload,
                 notify_url, hmac_secret, status, next_retry_at, attempt)
            VALUES ('wri_alert', NULL, $1, 'wri.threshold_crossed', $2,
                    $3, $4, 'pending', NOW(), 1)
        """, alert["id"], body, alert["notify_url"], alert["hmac_secret"])

    logger.info(
        "wri_alert enqueued alert=%s service=%s new_wri=%.1f → %s",
        alert["id"], svc_slug, new_wri, _log_safe_url(alert["notify_url"]),
    )
    return True


async def _deliver_wri_alert(
    pool, alert, entry, fired_at, fired_at_iso, timestamp
) -> bool:
    """Build payload, sign, POST, record. Returns True on 2xx.

    DEPRECATED in v0.8.0 — kept for compatibility with any direct callers /
    tests that exercise the synchronous-delivery path. New code should call
    _enqueue_wri_alert so failures get retried by _webhook_retry_loop.
    """
    old_wri = entry.get("old_wri")
    new_wri = entry.get("new_wri")
    total_signals = entry.get("total_signals", 0)
    svc_slug = entry.get("service", "")
    svc_category = entry.get("category") or ""
    pay_rate = entry.get("payment_rate", 0)
    threshold = float(alert["threshold_score"])

    svc_name = SERVICE_DISPLAY_NAMES.get(svc_slug, svc_slug)
    is_managed = svc_slug in SERVICE_CONFIGS
    is_new = old_wri is None

    if is_new:
        msg = (f"A new service ({svc_name}) just entered WayforthRank above your "
               f"threshold of {threshold}. Current score: {round(new_wri, 1)} "
               f"({total_signals} signals, {pay_rate}% payment conversion).")
    else:
        msg = (f"{svc_name} crossed your WRI alert threshold of {threshold}. "
               f"Current score: {round(new_wri, 1)} "
               f"({total_signals} signals, {pay_rate}% payment conversion).")

    payload = {
        "event": "wri.threshold_crossed",
        "service": {
            "slug": svc_slug,
            "name": svc_name,
            "category": svc_category,
            "old_wri": round(old_wri, 2) if old_wri is not None else None,
            "new_wri": round(new_wri, 2),
            "total_signals": total_signals,
            "payment_rate": pay_rate,
            "managed": is_managed,
            "zero_setup": is_managed,
        },
        "alert": {
            "id": str(alert["id"]),
            "threshold_score": threshold,
            "category": alert["category"],
        },
        "fired_at": fired_at_iso,
        "message": msg,
    }
    body = json_lib.dumps(payload)

    if not alert["hmac_secret"]:
        logger.error(
            "wri_alert missing hmac_secret alert=%s — skipping delivery; recreate the alert",
            alert["id"],
        )
        return False
    sig = hmac.new(
        alert["hmac_secret"].encode(),
        f"{timestamp}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()

    status_code = None
    success = False
    try:
        from core.url_validation import validate_external_url
        validate_external_url(alert["notify_url"], field_name="notify_url")
    except Exception as _vexc:
        logger.warning("wri_alert refused alert=%s url=%s: %s",
                       alert["id"], _log_safe_url(alert["notify_url"]), _vexc)
        return False
    try:
        from core.url_validation import request_pinned
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            # EXEC-1: pin to the validated IP so a DNS rebind between the check
            # above and connect can't redirect this server-side POST to an
            # internal/metadata target.
            resp = await request_pinned(
                client, "POST", alert["notify_url"],
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Wayforth-Event": "wri.threshold_crossed",
                    "X-Wayforth-Timestamp": timestamp,
                    "X-Wayforth-Signature": f"sha256={sig}",
                },
                field_name="notify_url",
            )
        status_code = resp.status_code
        success = 200 <= status_code < 300
    except Exception as exc:
        logger.warning("wri_alert delivery failed alert=%s url=%s: %s",
                       alert["id"], _log_safe_url(alert["notify_url"]), exc)

    async with pool.acquire() as db:
        await db.execute("""
            UPDATE wri_alerts
            SET last_fired_at = $1, fired_count = fired_count + 1
            WHERE id = $2
        """, fired_at, alert["id"])
        await db.execute("""
            INSERT INTO wri_alert_logs
            (alert_id, service_slug, old_wri, new_wri, fired_at, response_status, success)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """, alert["id"], svc_slug,
            round(old_wri, 2) if old_wri is not None else None,
            round(new_wri, 2), fired_at, status_code, success)

    if success:
        logger.info("wri_alert fired alert=%s service=%s new_wri=%.1f → %s %d",
                    alert["id"], svc_slug, new_wri, _log_safe_url(alert["notify_url"]), status_code)
    else:
        logger.warning("wri_alert failed alert=%s service=%s status=%s",
                       alert["id"], svc_slug, status_code)
    return success


async def _fire_wri_alerts(pool, scored: list[dict]) -> int:
    """Check wri_alerts against updated scores and POST to matching notify_urls.

    scored: list of {service, old_wri, new_wri, payment_rate, total_signals, category}
    Returns count of alerts fired.

    P7 (v0.7.8): deliveries run in parallel via asyncio.gather rather than
    sequentially. With an 8s per-alert timeout, 50 alerts used to take up to
    400s; now bounded by the slowest single delivery. Each alert can still
    only fire once per batch (cooldown preserved via seen-set).
    """
    import time as _time
    if not pool or not scored:
        return 0

    fired_at = datetime.now(timezone.utc)
    fired_at_iso = fired_at.isoformat()
    timestamp = str(int(_time.time()))

    async with pool.acquire() as db:
        alerts = await db.fetch("""
            SELECT a.id, a.api_key_id, a.category, a.threshold_score, a.min_signals,
                   a.notify_url, a.last_fired_at, a.hmac_secret
            FROM wri_alerts a
            WHERE a.active = true
        """)

    matched: list[tuple] = []
    seen_alerts: set = set()
    for entry in scored:
        old_wri = entry.get("old_wri")
        new_wri = entry.get("new_wri")
        total_signals = entry.get("total_signals", 0)
        svc_category = entry.get("category") or ""

        if new_wri is None:
            continue
        for alert in alerts:
            if alert["id"] in seen_alerts:
                continue  # already queued this alert in this batch
            threshold = float(alert["threshold_score"])
            if new_wri < threshold:
                continue
            if old_wri is not None and old_wri >= threshold:
                continue
            if alert["category"] and alert["category"] != svc_category:
                continue
            if total_signals < (alert["min_signals"] or 1):
                continue
            if alert["last_fired_at"]:
                elapsed = (fired_at - alert["last_fired_at"].replace(tzinfo=timezone.utc)).total_seconds()
                if elapsed < 86400:
                    continue
            matched.append((alert, entry))
            seen_alerts.add(alert["id"])

    if not matched:
        return 0

    # v0.8.0 Item 5: enqueue rather than deliver. The generic webhook retry
    # worker picks these up within ~60s and handles HMAC + POST + retry +
    # exponential backoff. A single failed network round-trip no longer drops
    # the alert.
    results = await asyncio.gather(
        *[_enqueue_wri_alert(pool, alert, entry, fired_at, fired_at_iso)
          for (alert, entry) in matched],
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)


@router.post("/webhooks/register")
@limiter.limit("5/minute")
async def register_webhook(request: Request, body: WebhookRegistration, db=Depends(get_db)):
    """Register a webhook to receive events for your account."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)
    require_tier(_tier, "webhooks")

    webhook_url = body.resolved_url
    validate_external_url(webhook_url, field_name="url")

    # Lock contact_email to the authenticated user — callers cannot register webhooks for others
    owner = await db.fetchrow(
        "SELECT owner_email FROM api_keys WHERE user_id=$1::uuid AND active=true LIMIT 1", user_id
    )
    contact_email = owner["owner_email"] if owner else body.contact_email

    secret = secrets.token_hex(32)
    row = await db.fetchrow("""
        INSERT INTO provider_webhooks
        (service_id, webhook_url, contact_email, events, secret_token)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (service_id, webhook_url) DO UPDATE
        SET active = TRUE, secret_token = EXCLUDED.secret_token
        RETURNING id, webhook_url, events, active, created_at, last_fired_at
    """, body.service_id or "*", webhook_url, contact_email, body.events, secret)
    return {
        "id": str(row["id"]),
        "webhook_id": str(row["id"]),
        "url": row["webhook_url"],
        "events": row["events"],
        "active": row["active"],
        "last_fired_at": row["last_fired_at"].isoformat() if row["last_fired_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "secret_token": secret,
        "message": "Webhook registered. Store your secret_token — it won't be shown again.",
    }


@router.get("/webhooks")
@limiter.limit("30/minute")
async def list_webhooks(request: Request, db=Depends(get_db)):
    """List all active webhooks registered by the authenticated user."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)
    require_tier(_tier, "webhooks")

    owner = await db.fetchrow(
        "SELECT owner_email FROM api_keys WHERE user_id=$1::uuid AND active=true LIMIT 1", user_id
    )
    if not owner:
        return {"webhooks": [], "total": 0}

    rows = await db.fetch("""
        SELECT id, webhook_url, events, active, last_fired_at, created_at
        FROM provider_webhooks
        WHERE contact_email = $1 AND active = true
        ORDER BY created_at DESC
    """, owner["owner_email"])

    webhooks = [
        {
            "id": str(r["id"]),
            "url": r["webhook_url"],
            "events": list(r["events"]) if r["events"] else [],
            "active": r["active"],
            "last_fired_at": r["last_fired_at"].isoformat() if r["last_fired_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return {"webhooks": webhooks, "total": len(webhooks)}


# WRI alert webhooks — must be before /webhooks/{webhook_id} (static before parameterized)

@router.post("/webhooks/wri-alerts", tags=["Webhooks"])
@limiter.limit("20/minute")
async def create_wri_alert(request: Request, db=Depends(get_db)):
    """Register a webhook that fires when any service crosses a WRI score threshold."""
    # Session-OR-key (PR #25 pattern): the dashboard alerts UI uses the wf_session
    # cookie; API clients still send X-Wayforth-API-Key. resolve_dashboard_caller
    # accepts both and yields the same (user_id, api_key_id, tier).
    caller = await resolve_dashboard_caller(request, db)
    user_id, api_key_id, _tier = caller["user_id"], caller["api_key_id"], caller["tier"]
    require_tier(_tier, "wri_alerts")

    body = await request.json()
    threshold = body.get("threshold_score")
    category = body.get("category") or None
    min_signals = int(body.get("min_signals") or 5)
    notify_url = (body.get("notify_url") or "").strip()

    if threshold is None or not (50.0 <= float(threshold) <= 99.9):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_threshold",
            "message": "threshold_score must be between 50.0 and 99.9",
        })
    if category and category not in _VALID_ALERT_CATEGORIES:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_category",
            "valid": sorted(_VALID_ALERT_CATEGORIES),
        })
    if not (1 <= min_signals <= 100):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_min_signals",
            "message": "min_signals must be between 1 and 100",
        })
    validate_external_url(notify_url, field_name="notify_url")

    alert_secret = secrets.token_hex(32)  # 256 bits, distinct from api_key_id
    row = await db.fetchrow("""
        INSERT INTO wri_alerts
            (api_key_id, category, threshold_score, min_signals, notify_url, hmac_secret)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, category, threshold_score, min_signals, notify_url, active, created_at
    """, api_key_id, category, float(threshold), min_signals, notify_url, alert_secret)

    return {
        "id": str(row["id"]),
        "threshold_score": float(row["threshold_score"]),
        "category": row["category"],
        "min_signals": row["min_signals"],
        "notify_url": row["notify_url"],
        "active": row["active"],
        "created_at": row["created_at"].isoformat(),
        # Returned ONCE — store it; we sign every wri.threshold_crossed payload
        # with this. Verify with HMAC-SHA256 over `"{timestamp}.{body}"`.
        "hmac_secret": alert_secret,
    }


@router.get("/webhooks/wri-alerts", tags=["Webhooks"])
@limiter.limit("30/minute")
async def list_wri_alerts(request: Request, db=Depends(get_db)):
    """List all WRI alert webhooks registered to this API key."""
    # Session-OR-key (PR #25 pattern): the dashboard alerts UI uses the wf_session
    # cookie; API clients still send X-Wayforth-API-Key. resolve_dashboard_caller
    # accepts both and yields the same (user_id, api_key_id, tier).
    caller = await resolve_dashboard_caller(request, db)
    user_id, api_key_id, _tier = caller["user_id"], caller["api_key_id"], caller["tier"]
    require_tier(_tier, "wri_alerts")

    rows = await db.fetch("""
        SELECT id, category, threshold_score, min_signals, notify_url,
               active, created_at, last_fired_at, fired_count
        FROM wri_alerts
        WHERE api_key_id = $1 AND active = true
        ORDER BY created_at DESC
    """, api_key_id)

    return {
        "alerts": [
            {
                "id": str(r["id"]),
                "threshold_score": float(r["threshold_score"]),
                "category": r["category"],
                "min_signals": r["min_signals"],
                "notify_url": r["notify_url"],
                "active": r["active"],
                "created_at": r["created_at"].isoformat(),
                "last_fired_at": r["last_fired_at"].isoformat() if r["last_fired_at"] else None,
                "fired_count": r["fired_count"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.delete("/webhooks/wri-alerts/{alert_id}", tags=["Webhooks"])
@limiter.limit("20/minute")
async def delete_wri_alert(request: Request, alert_id: str, db=Depends(get_db)):
    """Deactivate a WRI alert webhook by ID."""
    # Session-OR-key (PR #25 pattern): the dashboard alerts UI uses the wf_session
    # cookie; API clients still send X-Wayforth-API-Key. resolve_dashboard_caller
    # accepts both and yields the same (user_id, api_key_id, tier).
    caller = await resolve_dashboard_caller(request, db)
    user_id, api_key_id, _tier = caller["user_id"], caller["api_key_id"], caller["tier"]
    require_tier(_tier, "wri_alerts")

    row = await db.fetchrow(
        "SELECT id FROM wri_alerts WHERE id = $1::uuid AND api_key_id = $2 AND active = true",
        alert_id, api_key_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "alert_not_found",
            "message": "No active WRI alert found with this ID under your API key.",
        })
    await db.execute(
        "UPDATE wri_alerts SET active = false WHERE id = $1::uuid", alert_id
    )
    return {"id": alert_id, "status": "deactivated"}


@router.get("/webhooks/{webhook_id}/deliveries", tags=["Webhooks"])
@limiter.limit("30/minute")
async def list_webhook_deliveries(
    request: Request,
    webhook_id: str,
    db=Depends(get_db),
    limit: int = 20,
    offset: int = 0,
):
    """List delivery attempts for a webhook (newest first)."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "api_key_required"})
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)

    owner = await db.fetchrow(
        "SELECT owner_email FROM api_keys WHERE user_id = $1 AND active = true LIMIT 1", user_id
    )
    webhook = await db.fetchrow(
        "SELECT id, contact_email FROM provider_webhooks WHERE id = $1::uuid",
        webhook_id,
    )
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if not owner or webhook["contact_email"] != owner["owner_email"]:
        raise HTTPException(status_code=403, detail="Not authorized to view this webhook")

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    rows = await db.fetch("""
        SELECT id, event, attempt, status, response_status, error,
               next_retry_at, last_attempted_at, created_at
        FROM webhook_deliveries
        WHERE webhook_id = $1::uuid
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """, webhook_id, limit, offset)

    return {
        "webhook_id": webhook_id,
        "deliveries": [
            {
                "id": str(r["id"]),
                "event": r["event"],
                "attempt": r["attempt"],
                "status": r["status"],
                "response_status": r["response_status"],
                "error": r["error"],
                "next_retry_at": r["next_retry_at"].isoformat() if r["next_retry_at"] else None,
                "last_attempted_at": r["last_attempted_at"].isoformat() if r["last_attempted_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post("/webhooks/{webhook_id}/retry", tags=["Webhooks"])
@limiter.limit("10/minute")
async def retry_webhook(request: Request, webhook_id: str, db=Depends(get_db)):
    """Re-enable a suspended webhook and queue a new delivery attempt."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "api_key_required"})
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)

    owner = await db.fetchrow(
        "SELECT owner_email FROM api_keys WHERE user_id = $1 AND active = true LIMIT 1", user_id
    )
    webhook = await db.fetchrow(
        "SELECT id, contact_email, suspended_at FROM provider_webhooks WHERE id = $1::uuid",
        webhook_id,
    )
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if not owner or webhook["contact_email"] != owner["owner_email"]:
        raise HTTPException(status_code=403, detail="Not authorized to retry this webhook")
    if not webhook["suspended_at"]:
        raise HTTPException(status_code=400, detail="Webhook is not suspended")

    # Get the last dead delivery to replay its event/payload
    last = await db.fetchrow("""
        SELECT event, payload FROM webhook_deliveries
        WHERE webhook_id = $1::uuid AND status = 'dead'
        ORDER BY created_at DESC LIMIT 1
    """, webhook_id)

    await db.execute(
        "UPDATE provider_webhooks SET suspended_at = NULL WHERE id = $1::uuid", webhook_id
    )
    if last:
        await db.execute("""
            INSERT INTO webhook_deliveries
              (webhook_id, event, payload, attempt, status, next_retry_at)
            VALUES ($1::uuid, $2, $3, 1, 'pending', NOW())
        """, webhook_id, last["event"], last["payload"])

    logger.info("Webhook %s manually retried by user %s", webhook_id, user_id)
    return {"status": "queued", "webhook_id": webhook_id}


@router.delete("/webhooks/{webhook_id}")
@limiter.limit("10/minute")
async def delete_webhook(request: Request, webhook_id: str, db=Depends(get_db)):
    """Deactivate a registered webhook. Auth: wf_session cookie OR the API key of the registrant."""
    # Session-OR-key (PR #25 pattern): dashboard webhook management uses the cookie.
    caller = await resolve_dashboard_caller(request, db)
    user_id, _api_key_id, _tier = caller["user_id"], caller["api_key_id"], caller["tier"]

    owner = await db.fetchrow(
        "SELECT owner_email FROM api_keys WHERE user_id = $1 AND active = true LIMIT 1", user_id
    )
    webhook = await db.fetchrow(
        "SELECT id, contact_email FROM provider_webhooks WHERE id = $1::uuid AND active = true",
        webhook_id,
    )
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if not owner or webhook["contact_email"] != owner["owner_email"]:
        raise HTTPException(status_code=403, detail="Not authorized to delete this webhook")

    await db.execute(
        "UPDATE provider_webhooks SET active = FALSE WHERE id = $1::uuid", webhook_id
    )
    return {"webhook_id": webhook_id, "status": "deactivated"}
