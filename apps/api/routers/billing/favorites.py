"""routers/billing/favorites.py — POST/GET/DELETE /account/favorites."""

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.db import get_db
from core.rate_limit import limiter

router = APIRouter()


class FavoriteRequest(BaseModel):
    slug: str


def _auth_key_hash(request: Request) -> str:
    raw = request.headers.get("X-Wayforth-API-Key", "")
    if not raw:
        raise HTTPException(status_code=401, detail="API key required")
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_user_id(db, key_hash: str) -> str:
    row = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true", key_hash
    )
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return str(row["user_id"])


@router.post("/account/favorites", status_code=201)
@limiter.limit("30/minute")
async def add_favorite(body: FavoriteRequest, request: Request, db=Depends(get_db)):
    user_id = await _get_user_id(db, _auth_key_hash(request))

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
    user_id = await _get_user_id(db, _auth_key_hash(request))

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
    user_id = await _get_user_id(db, _auth_key_hash(request))

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
