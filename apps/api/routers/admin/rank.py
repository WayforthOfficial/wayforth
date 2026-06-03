"""routers/admin/rank.py — /admin/rank/* routes."""

import logging
import os
import secrets

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from core.db import get_db
from routers.webhooks import _fire_wri_alerts

logger = logging.getLogger("wayforth")

router = APIRouter()


@router.post("/admin/rank/recalculate", tags=["Admin"])
async def rank_recalculate(request: Request, db=Depends(get_db)):
    """Recompute WayforthRank v2 scores for all services with payment signal data.

    Proxies to the wayforth-rank private service (RANK_SERVICE_URL) so the
    v2 formula weights stay out of this public container."""
    from main import app, ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    rank_url = os.environ.get("RANK_SERVICE_URL", "")
    rank_key = os.environ.get("RANK_SERVICE_KEY", "")
    if not rank_url:
        return JSONResponse({"error": "rank_service_not_configured"}, status_code=503)

    # Pass the managed slug list so the rank service can base-only-score managed
    # services that have no usage signal yet (TASK 1). Sourced from
    # SERVICE_CONFIGS so it never drifts and never base-scores the whole catalog.
    try:
        from services.managed import SERVICE_CONFIGS
        _managed_slugs = list(SERVICE_CONFIGS.keys())
    except Exception:
        _managed_slugs = []

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{rank_url}/v1/rank/recalculate",
                headers={"X-Rank-Service-Key": rank_key},
                json={"managed_slugs": _managed_slugs},
            )
            resp.raise_for_status()
            results = resp.json()
    except Exception as exc:
        logger.error("rank recalculate proxy failed: %s", exc)
        return JSONResponse(
            {"error": "rank_service_unavailable", "detail": str(exc)},
            status_code=502,
        )

    alerts_fired = await _fire_wri_alerts(app.state.pool, results.get("scores", []))
    return {
        "updated": results.get("updated", 0),
        "scores": results.get("scores", []),
        "alerts_fired": alerts_fired,
        "unmatched_slugs": results.get("unmatched_slugs", []),
    }
