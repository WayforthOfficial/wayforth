"""tests/test_account_auth.py — Tri-mode dashboard auth on /account/* endpoints.

The bug we are guarding against: after the v0.7.0 cookie work, the dashboard
calls /auth/session to set wf_session and no longer holds the API key, so
/account/tier (and the rest) silently 401'd and the UI defaulted to "Free".

The fix is `core.auth.resolve_dashboard_caller`, which accepts (in priority):
  1. wf_session cookie  (browser dashboard)
  2. Authorization: Bearer <supabase_jwt>  (legacy / non-browser)
  3. X-Wayforth-API-Key  (programmatic clients)

These tests assert all three paths return the same shape, and that priority
follows the order above.

Pure unit — no live HTTP / DB / Redis. Endpoint handlers are invoked directly
with mocked db; slowapi wrappers are stripped session-wide.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.session import _SCOPE_KEY_RECORD, _SCOPE_KEY_TOKEN  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Unwrap slowapi decorators session-wide (same trick as tests/test_mfa.py).
# Every /account/* endpoint we touch is wrapped with @limiter.limit; the
# decorator validates the `request` arg is a real Starlette Request, which
# blocks direct unit-test calls with MagicMock stand-ins.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True, scope="session")
def _unwrap_account_endpoints():
    import routers.auth as _auth
    import routers.billing.account as _a
    import routers.billing.favorites as _f
    import routers.billing.referrals as _r
    saved = {}
    targets = [
        # /account/api-key lives in routers.auth (not /account/* in billing).
        # The login flow hits it right after /auth/session, so it must accept
        # the cookie + Bearer + API key paths the same as the rest of
        # /account/*.
        (_auth, ["get_api_key"]),
        (_a, ["account_credits", "account_tier", "account_analytics",
              "account_searches", "account_executions",
              "account_usage_history", "account_agents", "account_agent_detail",
              "account_alerts", "account_org", "account_founding_status",
              "get_billing_permissions", "put_billing_permissions"]),
        (_f, ["add_favorite", "remove_favorite", "list_favorites"]),
        (_r, ["get_referral", "redeem_referral"]),
    ]
    for mod, names in targets:
        for name in names:
            fn = getattr(mod, name, None)
            if fn and hasattr(fn, "__wrapped__"):
                saved[(mod, name)] = fn
                setattr(mod, name, fn.__wrapped__)
    yield
    for (mod, name), fn in saved.items():
        setattr(mod, name, fn)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _req(headers: dict | None = None, scope: dict | None = None) -> MagicMock:
    """Build a request-like object suitable for the unwrapped endpoint handlers
    AND for resolve_dashboard_caller (which reads request.scope for cookies)."""
    r = MagicMock()
    r.headers = headers or {}
    r.scope = {"type": "http"}
    if scope:
        r.scope.update(scope)
    return r


def _cookie_scope(user_id: str = "uid-1") -> dict:
    """Mimic what SessionCookieMiddleware stashes after validating wf_session."""
    return {
        _SCOPE_KEY_RECORD: {
            "user_id": user_id,
            "email": "user@example.com",
            "tier": "pro",
            "supabase_id": "sub-1",
        },
        _SCOPE_KEY_TOKEN: "opaque-token",
    }


def _bearer_headers(token: str = "fake.jwt.token") -> dict:
    return {"Authorization": f"Bearer {token}"}


def _api_key_headers(raw_key: str = "wf_live_" + "a" * 40) -> dict:
    return {"X-Wayforth-API-Key": raw_key}


def _user_row(user_id: str = "uid-1", tier: str = "pro",
              email: str = "user@example.com", api_key_id: str = "key-1") -> dict:
    """Shape returned by _load_dashboard_user / direct api_key lookup."""
    return {
        "user_id": user_id,
        "api_key_id": api_key_id,
        "tier": tier,
        "monthly_calls_count": 1234,
        "monthly_calls_reset_at": None,
        "email": email,
    }


# Patch target for the DB row that `_load_dashboard_user` and the API-key
# branch both ultimately read.
def _patch_db_for_caller(user_id="uid-1", tier="pro", api_key_id="key-1",
                         email="user@example.com",
                         credits_balance=9000, lifetime_credits=72000,
                         package_tier="pro"):
    """Return an AsyncMock db whose fetchrow / fetchval responses cover every
    SELECT the dashboard endpoints + resolver issue."""
    db = AsyncMock()

    # _load_dashboard_user does this fetchrow (cookie/JWT path)
    user_row = {
        "api_key_id": api_key_id,
        "tier": tier,
        "monthly_calls_count": 1234,
        "monthly_calls_reset_at": None,
        "email": email,
    }
    # api-key direct path
    key_row = {
        "user_id": user_id,
        "api_key_id": api_key_id,
        "tier": tier,
        "monthly_calls_count": 1234,
        "monthly_calls_reset_at": None,
        "email": email,
    }
    # user_credits row
    credits_row = {
        "credits_balance": credits_balance,
        "lifetime_credits": lifetime_credits,
        "package_tier": package_tier,
    }

    # Per-query dispatch by SQL substring. Endpoints issue different SELECTs
    # in a deterministic order; we return whichever row matches.
    async def _fetchrow(sql, *args):
        s = " ".join(sql.split())
        if "api_keys k WHERE k.key_hash" in s or "FROM api_keys k\n            JOIN" in s.replace("\n", "\n"):
            return key_row
        if "FROM api_keys k\n                JOIN" in s:
            return key_row
        if "FROM api_keys" in s and "WHERE k.key_hash" in s:
            return key_row
        # canonical key-by-hash queries
        if "WHERE k.key_hash" in s or "WHERE key_hash" in s:
            return key_row
        if "LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = TRUE" in s:
            return user_row
        if "FROM user_credits" in s:
            return credits_row
        if "FROM users WHERE id" in s:
            return {"founding_member": True, "founding_bonus_granted_at": None}
        return None

    async def _fetchval(sql, *args):
        s = " ".join(sql.split())
        if "SELECT id FROM users WHERE supabase_id" in s:
            return user_id
        if "FROM api_keys" in s and "monthly_calls_count" in s:
            return 1234
        return None

    db.fetchrow = AsyncMock(side_effect=_fetchrow)
    db.fetchval = AsyncMock(side_effect=_fetchval)
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value="UPDATE 1")
    return db


# ─────────────────────────────────────────────────────────────────────────────
# /account/tier — the original bug. Cookie, Bearer, API key all return same shape.
# ─────────────────────────────────────────────────────────────────────────────


class TestAccountTier:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_path_returns_tier(self):
        from routers.billing.account import account_tier
        db = _patch_db_for_caller(tier="pro", package_tier="pro", lifetime_credits=72000)
        resp = await account_tier(_req(scope=_cookie_scope()), db=db)
        assert resp["tier"] == "pro"
        assert resp["credits_total"] == 72000

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_bearer_jwt_path_returns_tier(self):
        from routers.billing.account import account_tier
        db = _patch_db_for_caller(tier="starter", package_tier="starter", lifetime_credits=21000)
        with patch("core.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-1", "email": "u@e"}):
            resp = await account_tier(_req(headers=_bearer_headers()), db=db)
        assert resp["tier"] == "starter"
        assert resp["credits_total"] == 21000

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_api_key_path_still_works(self):
        from routers.billing.account import account_tier
        db = _patch_db_for_caller(tier="builder", package_tier="builder", lifetime_credits=6000)
        resp = await account_tier(_req(headers=_api_key_headers()), db=db)
        assert resp["tier"] == "builder"
        assert resp["credits_total"] == 6000

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_no_auth_returns_401(self):
        from routers.billing.account import account_tier
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await account_tier(_req(), db=AsyncMock())
        assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# /account/credits — same three paths
# ─────────────────────────────────────────────────────────────────────────────


class TestAccountCredits:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_path_returns_credits(self):
        from routers.billing.account import account_credits
        db = _patch_db_for_caller(credits_balance=9000, lifetime_credits=72000,
                                  package_tier="pro", tier="pro")
        with patch("routers.billing.account.compute_calls_remaining",
                   AsyncMock(return_value=8500)):
            resp = await account_credits(_req(scope=_cookie_scope()), db=db)
        assert resp["credits_remaining"] == 9000
        assert resp["tier"] == "pro"
        assert resp["calls_remaining"] == 8500
        assert resp["email"] == "user@example.com"

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_bearer_path_returns_credits(self):
        from routers.billing.account import account_credits
        db = _patch_db_for_caller(tier="pro", package_tier="pro", lifetime_credits=72000)
        with patch("core.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-1", "email": "u@e"}), \
             patch("routers.billing.account.compute_calls_remaining",
                   AsyncMock(return_value=8500)):
            resp = await account_credits(_req(headers=_bearer_headers()), db=db)
        assert resp["credits_remaining"] == 9000


# ─────────────────────────────────────────────────────────────────────────────
# /account/searches and /account/executions — should both work via cookie
# ─────────────────────────────────────────────────────────────────────────────


class TestAccountReads:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_searches_via_cookie(self):
        from routers.billing.account import account_searches
        db = _patch_db_for_caller()
        # Override fetch for the searches list.
        db.fetch = AsyncMock(return_value=[])
        db.fetchval = AsyncMock(return_value=0)
        resp = await account_searches(_req(scope=_cookie_scope()), db=db)
        assert resp["total"] == 0
        assert resp["searches"] == []

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_executions_via_bearer(self):
        from routers.billing.account import account_executions
        db = _patch_db_for_caller()
        db.fetch = AsyncMock(return_value=[])
        # Keep the dispatcher fetchval — it returns user_id for the
        # supabase_id lookup AND 0 for the total-count query in the endpoint.
        with patch("core.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-1", "email": "u@e"}):
            resp = await account_executions(_req(headers=_bearer_headers()), db=db)
        assert resp["total"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Priority — cookie wins over Bearer wins over API key
# ─────────────────────────────────────────────────────────────────────────────


class TestPriority:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_wins_over_bearer_and_api_key(self):
        """If all three credentials are present, the cookie path runs. Verified
        by: only the cookie-path branch makes the LEFT JOIN api_keys query and
        never invokes verify_supabase_jwt nor reads X-Wayforth-API-Key."""
        from routers.billing.account import account_tier
        db = _patch_db_for_caller()
        with patch("core.auth.verify_supabase_jwt") as jwt_mock:
            resp = await account_tier(
                _req(headers={**_bearer_headers(), **_api_key_headers()},
                     scope=_cookie_scope()),
                db=db,
            )
        assert resp["tier"] == "pro"
        # JWT verifier must NOT have been called when a cookie was present.
        jwt_mock.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_bearer_wins_over_api_key(self):
        """No cookie but both Bearer + API key present → Bearer path runs."""
        from routers.billing.account import account_tier
        db = _patch_db_for_caller()
        with patch("core.auth.verify_supabase_jwt",
                   return_value={"sub": "sub-1", "email": "u@e"}) as jwt_mock:
            await account_tier(
                _req(headers={**_bearer_headers(), **_api_key_headers()}),
                db=db,
            )
        # JWT verifier MUST have been called (Bearer path) and not skipped.
        jwt_mock.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases on the resolver itself
# ─────────────────────────────────────────────────────────────────────────────


class TestResolverEdges:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_bearer_with_unknown_sub_returns_401(self):
        from core.auth import resolve_dashboard_caller
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchval = AsyncMock(return_value=None)  # no user with this supabase_id
        with patch("core.auth.verify_supabase_jwt", return_value={"sub": "sub-unknown"}):
            with pytest.raises(HTTPException) as exc:
                await resolve_dashboard_caller(_req(headers=_bearer_headers()), db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_bearer_with_invalid_jwt_returns_401(self):
        from core.auth import resolve_dashboard_caller
        from fastapi import HTTPException
        with patch("core.auth.verify_supabase_jwt", side_effect=Exception("bad sig")):
            with pytest.raises(HTTPException) as exc:
                await resolve_dashboard_caller(
                    _req(headers={"Authorization": "Bearer corrupt.jwt"}), AsyncMock()
                )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_invalid_api_key_returns_401(self):
        from core.auth import resolve_dashboard_caller
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await resolve_dashboard_caller(_req(headers=_api_key_headers()), db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_for_deleted_account_returns_401(self):
        """Session is valid (middleware passed) but the underlying user_id
        no longer exists — treat as logout, not 500."""
        from core.auth import resolve_dashboard_caller
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await resolve_dashboard_caller(_req(scope=_cookie_scope()), db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_user_without_active_api_key_still_resolves(self):
        """A user can exist in `users` with NO active row in `api_keys`
        (e.g. all keys deactivated). Dashboard endpoints must still load —
        the caller dict gets api_key_id=None and tier="free"."""
        from core.auth import _load_dashboard_user
        db = AsyncMock()
        # api_keys LEFT JOIN returns NULL for k.* fields when no active key.
        db.fetchrow = AsyncMock(return_value={
            "api_key_id": None,
            "tier": None,
            "monthly_calls_count": None,
            "monthly_calls_reset_at": None,
            "email": "stranded@example.com",
        })
        caller = await _load_dashboard_user(db, "uid-without-key")
        assert caller["api_key_id"] is None
        assert caller["tier"] == "free"
        assert caller["monthly_calls_count"] == 0
        assert caller["email"] == "stranded@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# favorites + referrals: cookie path
# ─────────────────────────────────────────────────────────────────────────────


class TestFavoritesCookiePath:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_list_favorites_via_cookie(self):
        from routers.billing.favorites import list_favorites
        db = _patch_db_for_caller()
        db.fetch = AsyncMock(return_value=[])
        resp = await list_favorites(_req(scope=_cookie_scope()), db=db)
        assert resp == {"favorites": []}

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_add_favorite_via_cookie(self):
        from routers.billing.favorites import add_favorite, FavoriteRequest
        db = _patch_db_for_caller()
        # First fetchval = COUNT(*); 2nd fetchrow = existing check
        db.fetchval = AsyncMock(return_value=0)

        async def _existing(sql, *args):
            return None
        db.fetchrow = AsyncMock(side_effect=_existing)
        db.execute = AsyncMock()
        # Patch the resolver because we replaced the helper-only fetchrow.
        with patch("routers.billing.favorites.resolve_dashboard_caller",
                   AsyncMock(return_value=_user_row())):
            resp = await add_favorite(
                FavoriteRequest(slug="groq"),
                _req(scope=_cookie_scope()),
                db=db,
            )
        assert resp == {"slug": "groq", "added": True}


class TestReferralsCookiePath:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_get_referral_via_cookie(self):
        from routers.billing.referrals import get_referral
        db = _patch_db_for_caller()
        # First fetchrow = existing referral lookup
        db.fetchrow = AsyncMock(return_value={"code": "WF-ABC123"})
        db.fetchval = AsyncMock(return_value=2)
        with patch("routers.billing.referrals.resolve_dashboard_caller",
                   AsyncMock(return_value=_user_row())):
            resp = await get_referral(_req(scope=_cookie_scope()), db=db)
        assert resp["referral_code"] == "WF-ABC123"
        assert resp["referrals_count"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# /account/api-key — regression: dashboard login was failing with
# "No API key returned for this account" when the user had no active api_key
# row. Authenticated users with no key should get {"api_key": null}, not 401.
# ─────────────────────────────────────────────────────────────────────────────


class TestAccountApiKey:

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_with_no_active_key_returns_null_not_401(self):
        """The bug: dashboard called /account/api-key right after /auth/session,
        got 401 'account_not_found' for any user without an active api_key, and
        treated it as a fatal login failure. Fixed: return {api_key: null} so
        the UI can offer to generate a key."""
        from routers.auth import get_api_key
        db = AsyncMock()
        # resolve_dashboard_caller returns api_key_id=None when the user
        # exists but has no active key (LEFT JOIN gave NULL).
        with patch("routers.auth.resolve_dashboard_caller", AsyncMock(return_value={
            "user_id": "uid-no-key",
            "api_key_id": None,
            "tier": "free",
            "monthly_calls_count": 0,
            "monthly_calls_reset_at": None,
            "email": "new@example.com",
        })):
            response = await get_api_key(_req(scope=_cookie_scope()), db=db)
        body = json.loads(response.body)
        assert body["api_key"] is None
        assert body["created_at"] is None
        assert body["last_used_at"] is None
        assert "message" in body
        # No DB query for encrypted_key should be issued when api_key_id is None.
        db.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_cookie_with_active_key_returns_decrypted_key(self):
        from routers.auth import get_api_key
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "key_prefix": "wf_live_abc",
            "encrypted_key": "ZmFrZS1lbmNyeXB0ZWQ=",  # value irrelevant — Fernet is patched below
            "created_at": None,
            "last_used_at": None,
        })

        class _FakeFernet:
            def decrypt(self, _b):
                return b"wf_live_RECOVERED_RAW_KEY"

        with patch("routers.auth.resolve_dashboard_caller", AsyncMock(return_value={
            "user_id": "uid-1",
            "api_key_id": "key-1",
            "tier": "pro",
            "monthly_calls_count": 0,
            "monthly_calls_reset_at": None,
            "email": "u@e",
        })), patch("routers.auth.get_fernet", return_value=_FakeFernet()):
            response = await get_api_key(_req(scope=_cookie_scope()), db=db)
        body = json.loads(response.body)
        assert body["api_key"] == "wf_live_RECOVERED_RAW_KEY"
        assert body["encrypted"] is True

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_legacy_key_without_encrypted_blob_returns_prefix_preview(self):
        """Keys issued before the encrypted-storage migration only have a
        prefix on file. We return a masked preview rather than null, with
        encrypted=false so the dashboard can prompt the user to rotate."""
        from routers.auth import get_api_key
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value={
            "key_prefix": "wf_live_xyz",
            "encrypted_key": None,
            "created_at": None,
            "last_used_at": None,
        })
        with patch("routers.auth.resolve_dashboard_caller", AsyncMock(return_value={
            "user_id": "uid-1",
            "api_key_id": "key-1",
            "tier": "free",
            "monthly_calls_count": 0,
            "monthly_calls_reset_at": None,
            "email": "u@e",
        })):
            response = await get_api_key(_req(scope=_cookie_scope()), db=db)
        body = json.loads(response.body)
        assert body["api_key"] == "wf_live_xyz..."
        assert body["encrypted"] is False

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_no_auth_returns_401(self):
        from routers.auth import get_api_key
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await get_api_key(_req(), db=AsyncMock())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_bearer_jwt_path_with_no_key_also_returns_null(self):
        """Same null-handling for the legacy JWT path — previously this
        branch also 401'd with account_not_found, breaking the login flow
        for any caller still on Bearer auth."""
        from routers.auth import get_api_key
        db = AsyncMock()
        # resolve_dashboard_caller will follow the Bearer branch internally;
        # we patch it to return the same "no api key" shape.
        with patch("routers.auth.resolve_dashboard_caller", AsyncMock(return_value={
            "user_id": "uid-2",
            "api_key_id": None,
            "tier": "free",
            "monthly_calls_count": 0,
            "monthly_calls_reset_at": None,
            "email": "u2@e",
        })):
            response = await get_api_key(_req(headers=_bearer_headers()), db=db)
        body = json.loads(response.body)
        assert body["api_key"] is None

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_key_revoked_between_resolve_and_fetch_returns_null(self):
        """Race: caller is resolved successfully but the api_key row
        disappears (or is deactivated) before the encrypted_key lookup.
        Must NOT 500 — return null so the dashboard recovers."""
        from routers.auth import get_api_key
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)  # key gone by the time we look it up
        with patch("routers.auth.resolve_dashboard_caller", AsyncMock(return_value={
            "user_id": "uid-3",
            "api_key_id": "key-soon-gone",
            "tier": "free",
            "monthly_calls_count": 0,
            "monthly_calls_reset_at": None,
            "email": "u@e",
        })):
            response = await get_api_key(_req(scope=_cookie_scope()), db=db)
        body = json.loads(response.body)
        assert body["api_key"] is None
