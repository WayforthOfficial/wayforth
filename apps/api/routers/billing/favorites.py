"""routers/billing/favorites.py — POST/GET/DELETE /account/favorites.

All three endpoints accept the dashboard's tri-mode auth (wf_session cookie,
Bearer JWT, or X-Wayforth-API-Key) via core.auth.resolve_dashboard_caller —
the cookie-only dashboard couldn't manage favorites otherwise."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import resolve_dashboard_caller
from core.db import get_db
from core.rate_limit import limiter

router = APIRouter()


class FavoriteRequest(BaseModel):
    slug: str


@router.post("/account/favorites", status_code=201)
@limiter.limit("30/minute")
async def add_favorite(body: FavoriteRequest, request: Request, db=Depends(get_db)):
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    count = await db.fetchval(
        "SELECT COUNT(*) FROM service_favorites WHERE user_id = $1::uuid", user_id
    )
    if count >= 50:
        raise HTTPException(status_code=422, detail="favorites_limit_reached")

    existing = await db.fetchrow(
        "SELECT 1 FROM service_favorites WHERE user_id = $1::uuid AND slug = $2",
        user_id, body.slug,
    )
    if existing:
        raise HTTPException(status_code=422, detail="already_favorited")

    await db.execute(
        "INSERT INTO service_favorites (user_id, slug, created_at) VALUES ($1::uuid, $2, NOW())",
        user_id, body.slug,
    )
    return {"slug": body.slug, "added": True}


@router.delete("/account/favorites/{slug}")
@limiter.limit("30/minute")
async def remove_favorite(slug: str, request: Request, db=Depends(get_db)):
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    result = await db.execute(
        "DELETE FROM service_favorites WHERE user_id = $1::uuid AND slug = $2",
        user_id, slug,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="favorite_not_found")
    return {"slug": slug, "removed": True}


@router.get("/account/favorites")
@limiter.limit("30/minute")
async def list_favorites(request: Request, db=Depends(get_db)):
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    rows = await db.fetch(
        """
        SELECT sf.slug, sf.created_at, s.name, s.description, s.endpoint_url AS url
        FROM service_favorites sf
        LEFT JOIN services s ON s.slug = sf.slug
        WHERE sf.user_id = $1::uuid
        ORDER BY sf.created_at DESC
        """,
        user_id,
    )
    return {"favorites": [dict(r) for r in rows]}
