"""routers/mfa.py — TOTP MFA for developer, provider, and admin dashboards."""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone

import bcrypt
import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.db import get_db
from core.rate_limit import limiter

logger = logging.getLogger("wayforth")

router = APIRouter(prefix="/auth/mfa", tags=["MFA"])

_CHALLENGE_TTL = timedelta(minutes=5)
_BACKUP_CHARS = string.ascii_uppercase + string.digits
_BACKUP_COUNT = 8
_BACKUP_LEN = 8

_TABLE = {"user": "users", "provider": "providers", "admin": "admin_users"}
_DASHBOARD = {"user": "developer", "provider": "provider", "admin": "admin"}
_ISSUER = {"developer": "Wayforth Developer", "provider": "Wayforth Provider", "admin": "Wayforth Admin"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dashboard_issuer(dashboard_type: str) -> str:
    return _ISSUER.get(dashboard_type, "Wayforth Developer")


async def _resolve_caller(request: Request, db) -> tuple[str, object, str, str, dict]:
    """Return (user_type, user_id, email, dashboard_type, row_dict)."""

    if request.headers.get("X-Admin-Token"):
        token_hash = hashlib.sha256(request.headers["X-Admin-Token"].encode()).hexdigest()
        row = await db.fetchrow("""
            SELECT u.id, u.email, u.password_hash, u.mfa_secret,
                   u.mfa_enabled, u.mfa_backup_codes, u.mfa_enabled_at
            FROM admin_sessions s
            JOIN admin_users u ON u.id = s.admin_user_id
            WHERE s.token_hash = $1 AND s.expires_at > NOW()
        """, token_hash)
        if not row:
            raise HTTPException(status_code=401, detail="Invalid admin session")
        return "admin", row["id"], row["email"], "admin", dict(row)

    if request.headers.get("X-Provider-Token"):
        token = request.headers["X-Provider-Token"]
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = await db.fetchrow("""
            SELECT p.id, p.email, p.password_hash, p.mfa_secret,
                   p.mfa_enabled, p.mfa_backup_codes, p.mfa_enabled_at
            FROM provider_sessions ps
            JOIN providers p ON p.id = ps.provider_id
            WHERE ps.token_hash = $1 AND ps.expires_at > NOW()
        """, token_hash)
        if not row:
            raise HTTPException(status_code=401, detail="Invalid provider session")
        return "provider", row["id"], row["email"], "provider", dict(row)

    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        row = await db.fetchrow("""
            SELECT u.id, u.email, u.mfa_secret, u.mfa_enabled,
                   u.mfa_backup_codes, u.mfa_enabled_at
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.key_hash = $1 AND k.active = TRUE
        """, key_hash)
        if not row:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return "user", row["id"], row["email"], "developer", dict(row)

    raise HTTPException(status_code=401, detail="Authentication required")


def _generate_backup_codes() -> list[str]:
    return [
        "".join(secrets.choice(_BACKUP_CHARS) for _ in range(_BACKUP_LEN))
        for _ in range(_BACKUP_COUNT)
    ]


def _hash_backup_code(code: str) -> str:
    return bcrypt.hashpw(code.upper().encode(), bcrypt.gensalt()).decode()


def _check_and_consume_backup_code(
    code: str, hashed_codes: list[str]
) -> tuple[bool, list[str]]:
    """Return (valid, remaining_codes). Removes matched code from list."""
    upper = code.upper()
    for i, hashed in enumerate(hashed_codes):
        try:
            if bcrypt.checkpw(upper.encode(), hashed.encode()):
                return True, hashed_codes[:i] + hashed_codes[i + 1 :]
        except Exception:
            continue
    return False, list(hashed_codes)


def _make_qr_data_uri(uri: str) -> str:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ── Models ────────────────────────────────────────────────────────────────────

class MFACodeBody(BaseModel):
    code: str


class MFAVerifyBody(BaseModel):
    code: str
    mfa_challenge: str


class MFADisableBody(BaseModel):
    code: str
    password: str = ""


class MFAResetBody(BaseModel):
    user_id: str
    user_type: str = "user"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/setup")
@limiter.limit("5/minute")
async def mfa_setup(request: Request, db=Depends(get_db)):
    """Generate TOTP secret and QR code. Does not enable MFA until verify-setup.

    Refuses if MFA is already enabled on this account — re-enrolling must go through
    /auth/mfa/disable first. Without this guard, an attacker holding a valid session
    could overwrite the legitimate user's secret and then disable MFA using a TOTP
    code from the new secret they control.
    """
    user_type, user_id, email, dashboard_type, row = await _resolve_caller(request, db)

    if row.get("mfa_enabled"):
        raise HTTPException(status_code=409, detail={
            "error": "mfa_already_enabled",
            "message": "MFA is already enabled. Disable it via /auth/mfa/disable before re-enrolling.",
        })

    secret = pyotp.random_base32()
    issuer = _dashboard_issuer(dashboard_type)
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)
    qr_code_url = _make_qr_data_uri(provisioning_uri)

    backup_codes_plain = _generate_backup_codes()
    backup_codes_hashed = [_hash_backup_code(c) for c in backup_codes_plain]

    table = _TABLE[user_type]
    await db.execute(
        f"UPDATE {table} SET mfa_secret = $1, mfa_backup_codes = $2 WHERE id = $3",
        secret, backup_codes_hashed, user_id,
    )

    return {
        "qr_code_url": qr_code_url,
        "secret": secret,
        "backup_codes": backup_codes_plain,
        "issuer": issuer,
        "account": email,
    }


@router.post("/verify-setup")
@limiter.limit("10/minute")
async def mfa_verify_setup(request: Request, body: MFACodeBody, db=Depends(get_db)):
    """Verify TOTP code and enable MFA on this account."""
    user_type, user_id, _email, _dt, row = await _resolve_caller(request, db)

    secret = row.get("mfa_secret")
    if not secret:
        raise HTTPException(status_code=400, detail="Call /auth/mfa/setup first")
    if row.get("mfa_enabled"):
        raise HTTPException(status_code=400, detail="MFA already enabled")

    if not pyotp.TOTP(secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")

    table = _TABLE[user_type]
    await db.execute(
        f"UPDATE {table} SET mfa_enabled = TRUE, mfa_enabled_at = NOW() WHERE id = $1",
        user_id,
    )
    return {"success": True, "mfa_enabled": True}


@router.post("/verify")
@limiter.limit("10/minute")
async def mfa_verify(request: Request, body: MFAVerifyBody, db=Depends(get_db)):
    """Complete login: verify TOTP code against a challenge token, return full session."""
    challenge_hash = hashlib.sha256(body.mfa_challenge.encode()).hexdigest()
    challenge = await db.fetchrow(
        "SELECT * FROM mfa_challenges WHERE token_hash = $1 AND expires_at > NOW() AND used = FALSE",
        challenge_hash,
    )
    if not challenge:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge")

    user_type = challenge["user_type"]
    user_id = challenge["user_id"]
    table = _TABLE[user_type]

    row = await db.fetchrow(
        f"SELECT mfa_secret, mfa_backup_codes FROM {table} WHERE id = $1", user_id
    )
    if not row or not row["mfa_secret"]:
        raise HTTPException(status_code=400, detail="MFA not configured")

    verified = pyotp.TOTP(row["mfa_secret"]).verify(body.code, valid_window=1)

    if not verified:
        backup_codes = list(row["mfa_backup_codes"] or [])
        valid, remaining = _check_and_consume_backup_code(body.code, backup_codes)
        if valid:
            await db.execute(
                f"UPDATE {table} SET mfa_backup_codes = $1 WHERE id = $2",
                remaining, user_id,
            )
            verified = True

    if not verified:
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    await db.execute("UPDATE mfa_challenges SET used = TRUE WHERE id = $1", challenge["id"])

    # Issue full session token
    if user_type == "admin":
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        await db.execute(
            "INSERT INTO admin_sessions (admin_user_id, token_hash, expires_at) VALUES ($1, $2, $3)",
            user_id, token_hash, expires_at,
        )
        return {"token": raw_token, "token_type": "admin", "expires_at": expires_at.isoformat()}

    if user_type == "provider":
        raw_token = "pvdr_" + secrets.token_hex(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        await db.execute(
            "INSERT INTO provider_sessions (provider_id, token_hash, expires_at) VALUES ($1, $2, $3)",
            user_id, token_hash, expires_at,
        )
        return {"token": raw_token, "token_type": "provider", "expires_at": expires_at.isoformat()}

    return {"success": True, "mfa_verified": True, "user_type": "developer"}


@router.post("/disable")
@limiter.limit("5/minute")
async def mfa_disable(request: Request, body: MFADisableBody, db=Depends(get_db)):
    """Disable MFA. Requires valid TOTP code.

    Provider/admin accounts must additionally confirm their password. Developer
    accounts (Supabase-managed, no local password) must present a valid Supabase
    Bearer JWT in the `Authorization` header — an attacker holding only the API
    key cannot disable MFA without separately compromising the Supabase session.
    """
    user_type, user_id, email, _dt, row = await _resolve_caller(request, db)

    if not row.get("mfa_enabled"):
        raise HTTPException(status_code=400, detail="MFA is not enabled")

    secret = row.get("mfa_secret")
    if not secret or not pyotp.TOTP(secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    password_hash = row.get("password_hash")
    if password_hash:
        if not body.password:
            raise HTTPException(status_code=400, detail="Password required")
        if not bcrypt.checkpw(body.password.encode(), password_hash.encode()):
            raise HTTPException(status_code=401, detail="Invalid password")
    elif user_type == "user":
        # Developer accounts have no local password — require a verified
        # session (browser cookie) OR a fresh Supabase Bearer JWT, in either
        # case bound to the same account whose MFA is being disabled.
        from core.session import get_request_session
        session = get_request_session(request)
        sub = ""
        if session and session.get("user_id") and str(session["user_id"]) == str(user_id):
            # Cookie path: the middleware already validated the session against
            # Redis. The session record's user_id matches the account being
            # modified, so we don't need a separate sub comparison.
            sub = (session.get("supabase_id") or "").strip()
        if not sub:
            from core.auth import verify_supabase_jwt
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                raise HTTPException(status_code=401, detail={
                    "error": "supabase_session_required",
                    "message": "Provide wf_session cookie or Authorization: Bearer <supabase_jwt> to disable MFA.",
                })
            token = auth_header.removeprefix("Bearer ").strip()
            try:
                claims = await verify_supabase_jwt(token)
                sub = (claims.get("sub") or "").strip()
                if not sub:
                    raise ValueError("no sub")
            except Exception:
                raise HTTPException(status_code=401, detail={"error": "invalid_supabase_token"})
            owner_sub = await db.fetchval(
                "SELECT supabase_id FROM users WHERE id = $1::uuid", user_id,
            )
            if not owner_sub or str(owner_sub).lower() != sub.lower():
                raise HTTPException(status_code=403, detail={
                    "error": "session_account_mismatch",
                    "message": "Supabase session does not match the account whose MFA is being disabled.",
                })

    table = _TABLE[user_type]
    await db.execute(
        f"""UPDATE {table}
            SET mfa_enabled = FALSE, mfa_secret = NULL,
                mfa_backup_codes = NULL, mfa_enabled_at = NULL
            WHERE id = $1""",
        user_id,
    )
    return {"success": True, "mfa_enabled": False}


@router.post("/reset")
async def mfa_reset(request: Request, body: MFAResetBody, db=Depends(get_db)):
    """Admin-only: clear MFA for any user (lockout recovery)."""
    from routers.admin.dashboard import get_admin_session
    session = await get_admin_session(request, db)
    if session.get("role") not in ("ceo", "support"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")

    if body.user_type not in _TABLE:
        raise HTTPException(status_code=400, detail="user_type must be 'user', 'provider', or 'admin'")

    table = _TABLE[body.user_type]
    result = await db.execute(
        f"""UPDATE {table}
            SET mfa_enabled = FALSE, mfa_secret = NULL,
                mfa_backup_codes = NULL, mfa_enabled_at = NULL
            WHERE id = $1::uuid""",
        body.user_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="User not found")

    logger.info("Admin %s reset MFA for %s:%s", session["email"], body.user_type, body.user_id)
    return {"success": True, "user_id": body.user_id, "user_type": body.user_type}


@router.get("/status")
@limiter.limit("20/minute")
async def mfa_status(request: Request, db=Depends(get_db)):
    """Return MFA status for the authenticated caller."""
    user_type, _uid, _email, dashboard_type, row = await _resolve_caller(request, db)
    enabled_at = row.get("mfa_enabled_at")
    return {
        "mfa_enabled": bool(row.get("mfa_enabled")),
        "mfa_enabled_at": enabled_at.isoformat() if enabled_at else None,
        "dashboard_type": dashboard_type,
    }


# ── Challenge issuance (used by login endpoints) ──────────────────────────────

async def issue_mfa_challenge(db, user_type: str, user_id) -> str:
    """Store a short-lived challenge token and return the raw token string."""
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + _CHALLENGE_TTL
    await db.execute(
        "INSERT INTO mfa_challenges (user_type, user_id, token_hash, expires_at) VALUES ($1, $2, $3, $4)",
        user_type, user_id, token_hash, expires_at,
    )
    return raw
