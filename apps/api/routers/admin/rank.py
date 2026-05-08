"""routers/admin/rank.py — /admin/rank/* routes."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from core.db import get_db
from routers.webhooks import _fire_wri_alerts

logger = logging.getLogger("wayforth")

router = APIRouter()


@router.post("/admin/rank/recalculate", tags=["Admin"])
async def rank_recalculate(request: Request, db=Depends(get_db)):
    """Recompute WayforthRank v2 scores for all services with payment signal data."""
    from main import app, ADMIN_KEY
    import secrets
    provided_key = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    from wayforth_rank_v2 import compute_wri_v2

    signal_rows = await db.fetch("""
        SELECT
            clicked_slug,
            COUNT(*) AS total_clicks,
            SUM(CASE WHEN payment_followed THEN 1 ELSE 0 END) AS payments,
            MAX(created_at) AS last_seen
        FROM search_analytics
        WHERE clicked_slug IS NOT NULL
        GROUP BY clicked_slug
    """)

    services = await db.fetch("SELECT id, name, category, wri_score FROM services")

    def _slug(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")

    def _norm(name: str) -> str:
        import re as _re
        return _re.sub(r'[^a-z0-9]', '', name.lower())

    svc_map = {_slug(s["name"]): s for s in services}
    norm_map = {_norm(s["name"]): s for s in services}

    results = []
    unmatched = []
    for sig in signal_rows:
        key = sig["clicked_slug"].lower().replace("-", "_")
        # Exact slug match, then prefix match, then normalized match
        svc = svc_map.get(key)
        if not svc:
            for svc_key, s in svc_map.items():
                if svc_key.startswith(key + "_"):
                    svc = s
                    break
        if not svc:
            norm_key = _norm(sig["clicked_slug"])
            svc = norm_map.get(norm_key)
        if not svc:
            for norm_svc_key, s in norm_map.items():
                if norm_svc_key.startswith(norm_key):
                    svc = s
                    break
        if not svc:
            unmatched.append({
                "clicked_slug": sig["clicked_slug"],
                "total_clicks": int(sig["total_clicks"] or 0),
                "payments": int(sig["payments"] or 0),
                "tried_key": key,
            })
            continue

        hist = await db.fetchrow(
            "SELECT wri_score FROM service_score_history "
            "WHERE service_id = $1 ORDER BY recorded_at DESC LIMIT 1",
            str(svc["id"])
        )
        base_wri = float(hist["wri_score"]) if hist else 60.0

        payments = int(sig["payments"] or 0)
        total_clicks = int(sig["total_clicks"] or 0)
        new_wri = compute_wri_v2(base_wri, payments, total_clicks, sig["last_seen"])
        pay_rate = round(payments * 100.0 / max(total_clicks, 1), 1)
        old_wri = float(svc["wri_score"]) if svc["wri_score"] is not None else None

        await db.execute(
            "UPDATE services SET wri_score = $1, wri_version = 'v2' WHERE id = $2",
            new_wri, svc["id"]
        )
        results.append({
            "service": sig["clicked_slug"],
            "old_wri": old_wri,
            "new_wri": new_wri,
            "payment_rate": pay_rate,
            "total_signals": total_clicks,
            "category": svc.get("category") or "",
        })

    alerts_fired = await _fire_wri_alerts(app.state.pool, results)
    return {
        "updated": len(results),
        "scores": results,
        "alerts_fired": alerts_fired,
        "unmatched_slugs": unmatched,
    }
