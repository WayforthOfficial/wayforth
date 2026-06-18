"""routers/org.py — POST /org/create, /org/invite, GET /org/members, /org/keys, DELETE /org/members/{id}."""

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.db import get_db
from core.rate_limit import limiter

router = APIRouter()


class OrgCreateRequest(BaseModel):
    name: str


class OrgInviteRequest(BaseModel):
    email: str
    role: str = "member"


# AUTHZ-3: roles an admin may assign via invite. 'owner' is deliberately excluded
# — ownership is set only at org creation and can never be granted by invitation.
_INVITABLE_ROLES = {"member", "admin"}


def _validated_invite_role(raw: str) -> str:
    """Normalise + allow-list an invite role (AUTHZ-3). Raises 422 on anything
    outside _INVITABLE_ROLES so a caller can never assign 'owner' or an arbitrary
    string."""
    role = (raw or "member").strip().lower()
    if role not in _INVITABLE_ROLES:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_role",
            "valid": sorted(_INVITABLE_ROLES),
            "message": "role must be 'member' or 'admin' (owner cannot be assigned via invite).",
        })
    return role


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


async def _get_user_org(db, user_id: str):
    return await db.fetchrow("""
        SELECT o.* FROM organizations o
        JOIN org_members m ON m.org_id = o.id
        WHERE m.user_id = $1::uuid
        ORDER BY m.joined_at
        LIMIT 1
    """, user_id)


async def _require_admin(db, user_id: str, org_id) -> None:
    """AUTHZ-4: the admin check MUST be scoped to the org being operated on.
    Previously it matched any membership row for the user, so a user who was
    admin in org X but only a member in the target org passed the check."""
    member = await db.fetchrow(
        "SELECT role FROM org_members WHERE user_id = $1::uuid AND org_id = $2",
        user_id, org_id,
    )
    if not member or member["role"] not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="org_admin_required")


@router.post("/org/create", status_code=201)
@limiter.limit("5/minute")
async def create_org(body: OrgCreateRequest, request: Request, db=Depends(get_db)):
    user_id = await _get_user_id(db, _auth_key_hash(request))
    org_id = uuid.uuid4()
    await db.execute(
        "INSERT INTO organizations (id, name, owner_user_id, created_at) VALUES ($1, $2, $3::uuid, NOW())",
        org_id, body.name, user_id,
    )
    await db.execute(
        "INSERT INTO org_members (org_id, user_id, role, joined_at) VALUES ($1, $2::uuid, 'admin', NOW())",
        org_id, user_id,
    )
    return {"id": str(org_id), "name": body.name, "owner_user_id": user_id}


@router.post("/org/invite", status_code=201)
@limiter.limit("10/minute")
async def invite_org_member(body: OrgInviteRequest, request: Request, db=Depends(get_db)):
    user_id = await _get_user_id(db, _auth_key_hash(request))
    org = await _get_user_org(db, user_id)
    if not org:
        raise HTTPException(status_code=404, detail="no_org_found")
    await _require_admin(db, user_id, org["id"])

    # AUTHZ-3: never trust the caller-supplied role verbatim.
    role = _validated_invite_role(body.role)

    invited = await db.fetchrow("SELECT id FROM users WHERE email = $1", body.email)
    if not invited:
        raise HTTPException(status_code=404, detail="user_not_found")

    clash = await db.fetchrow(
        "SELECT 1 FROM org_members WHERE org_id = $1 AND user_id = $2",
        org["id"], invited["id"],
    )
    if clash:
        raise HTTPException(status_code=422, detail="already_member")

    await db.execute(
        "INSERT INTO org_members (org_id, user_id, role, joined_at) VALUES ($1, $2, $3, NOW())",
        org["id"], invited["id"], role,
    )
    return {"invited": body.email, "role": role}


@router.get("/org/members")
@limiter.limit("30/minute")
async def list_org_members(request: Request, db=Depends(get_db)):
    user_id = await _get_user_id(db, _auth_key_hash(request))
    org = await _get_user_org(db, user_id)
    if not org:
        raise HTTPException(status_code=404, detail="no_org_found")

    rows = await db.fetch("""
        SELECT u.id, u.email, m.role, m.joined_at,
               ak.tier AS plan,
               ak.monthly_calls_count,
               COALESCE(uc.credits_balance, 0) AS credits_balance
        FROM org_members m
        JOIN users u ON u.id = m.user_id
        LEFT JOIN api_keys ak ON ak.user_id = u.id AND ak.active = true
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        WHERE m.org_id = $1
        ORDER BY m.joined_at
    """, org["id"])
    return {
        "org_id": str(org["id"]),
        "org_name": org["name"],
        "members": [dict(r) for r in rows],
    }


@router.get("/org/keys")
@limiter.limit("20/minute")
async def list_org_keys(request: Request, db=Depends(get_db)):
    user_id = await _get_user_id(db, _auth_key_hash(request))
    org = await _get_user_org(db, user_id)
    if not org:
        raise HTTPException(status_code=404, detail="no_org_found")
    await _require_admin(db, user_id, org["id"])

    rows = await db.fetch("""
        SELECT ak.id, LEFT(ak.key_hash, 8) AS key_prefix, ak.created_at, ak.active, u.email
        FROM api_keys ak
        JOIN users u ON u.id = ak.user_id
        JOIN org_members m ON m.user_id = u.id
        WHERE m.org_id = $1
        ORDER BY ak.created_at DESC
    """, org["id"])
    return {"org_id": str(org["id"]), "keys": [dict(r) for r in rows]}


@router.delete("/org/members/{member_user_id}")
@limiter.limit("10/minute")
async def remove_org_member(member_user_id: str, request: Request, db=Depends(get_db)):
    user_id = await _get_user_id(db, _auth_key_hash(request))
    org = await _get_user_org(db, user_id)
    if not org:
        raise HTTPException(status_code=404, detail="no_org_found")
    await _require_admin(db, user_id, org["id"])

    if str(org["owner_user_id"]) == member_user_id:
        raise HTTPException(status_code=422, detail="cannot_remove_owner")

    result = await db.execute(
        "DELETE FROM org_members WHERE org_id = $1 AND user_id = $2::uuid",
        org["id"], member_user_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="member_not_found")
    return {"removed": member_user_id}
