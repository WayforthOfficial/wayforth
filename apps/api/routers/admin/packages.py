"""routers/admin/packages.py — /admin/packages/* (package revocation flagging, Step 5)."""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from core.admin_auth import admin_authed
from core.db import get_db
from core.package_revocation import flagged_versions, revoke_package

router = APIRouter()
logger = logging.getLogger("wayforth")


@router.post("/admin/packages/revoke", tags=["Admin"])
async def revoke(request: Request, db=Depends(get_db)):
    """Revoke a (previously allowlisted) package and flag every version that baked it in.
    Body: {name, version?, reason?}. version omitted ⇒ all versions revoked."""
    if not await admin_authed(request, db):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=422)
    version, reason = body.get("version"), body.get("reason")
    async with db.transaction():
        flagged = await revoke_package(db, name, version=version, reason=reason)
    logger.warning("package revoked: %s%s (%d versions flagged)", name,
                   f"=={version}" if version else " (all versions)", flagged)
    return {"revoked": name, "version": version, "versions_flagged": flagged}


@router.get("/admin/packages/flagged", tags=["Admin"])
async def flagged(request: Request, db=Depends(get_db)):
    """Versions currently flagged as using a revoked package — for rebuild/triage."""
    if not await admin_authed(request, db):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"flagged_versions": await flagged_versions(db)}
