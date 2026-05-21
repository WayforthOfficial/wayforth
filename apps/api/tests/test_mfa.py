"""tests/test_mfa.py — TOTP MFA unit and integration test suite."""
from __future__ import annotations

import string
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import pytest_asyncio
import httpx
import os

BASE_URL = os.environ.get("WAYFORTH_TEST_BASE_URL", "https://gateway.wayforth.io")
API_KEY  = os.environ.get("WAYFORTH_TEST_API_KEY", "")


# ─────────────────────────────────────────────────────────────────────────────
# Session fixture: remove slowapi wrappers so endpoints can be called directly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def _unwrap_mfa_endpoints():
    """Strip @limiter.limit wrappers from MFA endpoint functions.

    slowapi requires a real starlette Request for its isinstance check.
    Direct unit-test calls use plain dicts, so we swap the wrappers out once.
    """
    import routers.mfa as _m
    _saved = {}
    for name in ("mfa_setup", "mfa_verify_setup", "mfa_verify", "mfa_disable", "mfa_status"):
        fn = getattr(_m, name, None)
        if fn and hasattr(fn, "__wrapped__"):
            _saved[name] = fn
            setattr(_m, name, fn.__wrapped__)
    yield
    for name, fn in _saved.items():
        setattr(_m, name, fn)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _req(headers: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = headers or {}
    return req


# ─────────────────────────────────────────────────────────────────────────────
# TestDashboardIssuer — pure unit, no network
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardIssuer:
    """_dashboard_issuer returns the correct QR-code issuer string per role."""

    def test_developer_label(self):
        from routers.mfa import _dashboard_issuer
        assert _dashboard_issuer("developer") == "Wayforth Developer"

    def test_provider_label(self):
        from routers.mfa import _dashboard_issuer
        assert _dashboard_issuer("provider") == "Wayforth Provider"

    def test_admin_label(self):
        from routers.mfa import _dashboard_issuer
        assert _dashboard_issuer("admin") == "Wayforth Admin"

    def test_unknown_defaults_to_developer(self):
        from routers.mfa import _dashboard_issuer
        assert _dashboard_issuer("unknown") == "Wayforth Developer"

    def test_empty_string_defaults_to_developer(self):
        from routers.mfa import _dashboard_issuer
        assert _dashboard_issuer("") == "Wayforth Developer"

    @pytest.mark.parametrize("dashboard_type,expected", [
        ("developer", "Wayforth Developer"),
        ("provider",  "Wayforth Provider"),
        ("admin",     "Wayforth Admin"),
    ])
    def test_all_three_labels(self, dashboard_type, expected):
        from routers.mfa import _dashboard_issuer
        assert _dashboard_issuer(dashboard_type) == expected


# ─────────────────────────────────────────────────────────────────────────────
# TestMFALabels — provisioning URI carries the correct issuer
# ─────────────────────────────────────────────────────────────────────────────

class TestMFALabels:
    """The provisioning URI embeds the correct issuer_name for each dashboard."""

    @pytest.mark.parametrize("dashboard_type,expected_issuer", [
        ("developer", "Wayforth Developer"),
        ("provider",  "Wayforth Provider"),
        ("admin",     "Wayforth Admin"),
    ])
    def test_issuer_param_in_provisioning_uri(self, dashboard_type, expected_issuer):
        import pyotp
        from routers.mfa import _dashboard_issuer
        secret = pyotp.random_base32()
        issuer = _dashboard_issuer(dashboard_type)
        uri = pyotp.TOTP(secret).provisioning_uri(name="u@example.com", issuer_name=issuer)
        qs = parse_qs(urlparse(uri).query)
        assert unquote(qs["issuer"][0]) == expected_issuer

    @pytest.mark.parametrize("dashboard_type,expected_issuer", [
        ("developer", "Wayforth Developer"),
        ("provider",  "Wayforth Provider"),
        ("admin",     "Wayforth Admin"),
    ])
    def test_issuer_in_uri_path_label(self, dashboard_type, expected_issuer):
        import pyotp
        from routers.mfa import _dashboard_issuer
        secret = pyotp.random_base32()
        issuer = _dashboard_issuer(dashboard_type)
        uri = pyotp.TOTP(secret).provisioning_uri(name="u@example.com", issuer_name=issuer)
        # issuer appears URL-encoded in the path
        assert unquote(uri).startswith(f"otpauth://totp/{expected_issuer}")

    def test_account_email_in_provisioning_uri(self):
        import pyotp
        from routers.mfa import _dashboard_issuer
        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(
            name="dev@example.com", issuer_name=_dashboard_issuer("developer")
        )
        assert "dev" in unquote(uri) and "example.com" in unquote(uri)


# ─────────────────────────────────────────────────────────────────────────────
# TestBackupCodes — generation and single-use consumption
# ─────────────────────────────────────────────────────────────────────────────

class TestBackupCodes:
    """Backup-code generation, hashing, and single-use consumption."""

    def test_generates_8_codes(self):
        from routers.mfa import _generate_backup_codes
        assert len(_generate_backup_codes()) == 8

    def test_each_code_is_8_chars(self):
        from routers.mfa import _generate_backup_codes
        for code in _generate_backup_codes():
            assert len(code) == 8

    def test_codes_are_uppercase_alphanumeric(self):
        from routers.mfa import _generate_backup_codes
        valid = set(string.ascii_uppercase + string.digits)
        for code in _generate_backup_codes():
            assert all(c in valid for c in code)

    def test_codes_are_unique(self):
        from routers.mfa import _generate_backup_codes
        codes = _generate_backup_codes()
        assert len(set(codes)) == 8

    def test_valid_backup_code_accepted(self):
        from routers.mfa import _generate_backup_codes, _hash_backup_code, _check_and_consume_backup_code
        codes = _generate_backup_codes()
        hashed = [_hash_backup_code(c) for c in codes]
        valid, remaining = _check_and_consume_backup_code(codes[0], hashed)
        assert valid is True
        assert len(remaining) == 7

    def test_backup_code_consumed_after_first_use(self):
        from routers.mfa import _generate_backup_codes, _hash_backup_code, _check_and_consume_backup_code
        codes = _generate_backup_codes()
        hashed = [_hash_backup_code(c) for c in codes]
        _, remaining = _check_and_consume_backup_code(codes[0], hashed)
        valid, _ = _check_and_consume_backup_code(codes[0], remaining)
        assert valid is False

    def test_invalid_backup_code_rejected(self):
        from routers.mfa import _generate_backup_codes, _hash_backup_code, _check_and_consume_backup_code
        codes = _generate_backup_codes()
        hashed = [_hash_backup_code(c) for c in codes]
        valid, remaining = _check_and_consume_backup_code("XXXXXXXX", hashed)
        assert valid is False
        assert len(remaining) == 8

    def test_backup_code_case_insensitive(self):
        from routers.mfa import _generate_backup_codes, _hash_backup_code, _check_and_consume_backup_code
        codes = _generate_backup_codes()
        hashed = [_hash_backup_code(c) for c in codes]
        valid, _ = _check_and_consume_backup_code(codes[0].lower(), hashed)
        assert valid is True

    def test_empty_backup_codes_list(self):
        from routers.mfa import _check_and_consume_backup_code
        valid, remaining = _check_and_consume_backup_code("AAAABBBB", [])
        assert valid is False
        assert remaining == []

    def test_only_one_code_consumed_per_use(self):
        from routers.mfa import _generate_backup_codes, _hash_backup_code, _check_and_consume_backup_code
        codes = _generate_backup_codes()
        hashed = [_hash_backup_code(c) for c in codes]
        _, after_first = _check_and_consume_backup_code(codes[0], hashed)
        _, after_second = _check_and_consume_backup_code(codes[1], after_first)
        assert len(after_second) == 6


# ─────────────────────────────────────────────────────────────────────────────
# TestMFASetup — endpoint behaviour (mocked DB + _resolve_caller)
# ─────────────────────────────────────────────────────────────────────────────

def _caller(user_type="user", dashboard_type="developer", mfa_enabled=False, mfa_secret=None):
    return (
        user_type, "uid-1", f"{user_type}@example.com", dashboard_type,
        {"mfa_secret": mfa_secret, "mfa_enabled": mfa_enabled,
         "mfa_backup_codes": None, "mfa_enabled_at": None, "password_hash": None},
    )


class TestMFASetup:
    """POST /auth/mfa/setup — generates secret + QR + backup codes without enabling."""

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_returns_secret(self):
        from routers.mfa import mfa_setup
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            result = await mfa_setup(_req(), db)
        assert "secret" in result and len(result["secret"]) > 10

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_returns_qr_code_data_uri(self):
        from routers.mfa import mfa_setup
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            result = await mfa_setup(_req(), db)
        assert result["qr_code_url"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_returns_8_backup_codes(self):
        from routers.mfa import mfa_setup
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            result = await mfa_setup(_req(), db)
        assert len(result["backup_codes"]) == 8

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_does_not_set_mfa_enabled(self):
        from routers.mfa import mfa_setup
        db = AsyncMock()
        executed_sql: list[str] = []
        async def _exec(sql, *args):
            executed_sql.append(sql)
        db.execute = _exec
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            await mfa_setup(_req(), db)
        assert not any("mfa_enabled = TRUE" in s for s in executed_sql)

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_secret_is_valid_base32(self):
        import pyotp
        from routers.mfa import mfa_setup
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            result = await mfa_setup(_req(), db)
        assert len(pyotp.TOTP(result["secret"]).now()) == 6

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    @pytest.mark.parametrize("user_type,dashboard_type,expected_issuer", [
        ("user",     "developer", "Wayforth Developer"),
        ("provider", "provider",  "Wayforth Provider"),
        ("admin",    "admin",     "Wayforth Admin"),
    ])
    async def test_issuer_per_dashboard_type(self, user_type, dashboard_type, expected_issuer):
        from routers.mfa import mfa_setup
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller(user_type, dashboard_type)):
            result = await mfa_setup(_req(), db)
        assert result["issuer"] == expected_issuer

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_account_matches_email(self):
        from routers.mfa import mfa_setup
        db = AsyncMock()
        db.execute = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            result = await mfa_setup(_req(), db)
        assert result["account"] == "user@example.com"


# ─────────────────────────────────────────────────────────────────────────────
# TestMFAVerifySetup — enabling MFA after scanning QR
# ─────────────────────────────────────────────────────────────────────────────

class TestMFAVerifySetup:
    """POST /auth/mfa/verify-setup — enables MFA on valid TOTP code."""

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_valid_code_enables_mfa(self):
        import pyotp
        from routers.mfa import mfa_verify_setup, MFACodeBody
        db = AsyncMock()
        db.execute = AsyncMock()
        secret = pyotp.random_base32()
        with patch("routers.mfa._resolve_caller", return_value=_caller(mfa_secret=secret)):
            result = await mfa_verify_setup(_req(), MFACodeBody(code=pyotp.TOTP(secret).now()), db)
        assert result["success"] is True
        assert result["mfa_enabled"] is True

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_invalid_code_returns_400(self):
        import pyotp
        from routers.mfa import mfa_verify_setup, MFACodeBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        with patch("routers.mfa._resolve_caller", return_value=_caller(mfa_secret=secret)):
            with pytest.raises(HTTPException) as exc:
                await mfa_verify_setup(_req(), MFACodeBody(code="000000"), db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_already_enabled_returns_400(self):
        import pyotp
        from routers.mfa import mfa_verify_setup, MFACodeBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        with patch("routers.mfa._resolve_caller", return_value=_caller(mfa_secret=secret, mfa_enabled=True)):
            with pytest.raises(HTTPException) as exc:
                await mfa_verify_setup(_req(), MFACodeBody(code="123456"), db)
        assert exc.value.status_code == 400
        assert "already" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_no_secret_returns_400(self):
        from routers.mfa import mfa_verify_setup, MFACodeBody
        from fastapi import HTTPException
        db = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            with pytest.raises(HTTPException) as exc:
                await mfa_verify_setup(_req(), MFACodeBody(code="123456"), db)
        assert exc.value.status_code == 400
        assert "setup" in str(exc.value.detail).lower()


# ─────────────────────────────────────────────────────────────────────────────
# TestMFAVerify — login challenge completion
# ─────────────────────────────────────────────────────────────────────────────

class TestMFAVerify:
    """POST /auth/mfa/verify — validates TOTP or backup code against a challenge token."""

    def _challenge(self, user_type="user", user_id="uid-1"):
        return {"user_type": user_type, "user_id": user_id, "id": "chal-1"}

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_valid_totp_developer_returns_success(self):
        import pyotp
        from routers.mfa import mfa_verify, MFAVerifyBody
        db = AsyncMock()
        secret = pyotp.random_base32()
        db.fetchrow = AsyncMock(side_effect=[
            self._challenge("user"),
            {"mfa_secret": secret, "mfa_backup_codes": []},
        ])
        db.execute = AsyncMock()
        result = await mfa_verify(_req(), MFAVerifyBody(code=pyotp.TOTP(secret).now(), mfa_challenge="tok"), db)
        assert result.get("mfa_verified") is True or "success" in result

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_invalid_totp_returns_401(self):
        import pyotp
        from routers.mfa import mfa_verify, MFAVerifyBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        db.fetchrow = AsyncMock(side_effect=[
            self._challenge("user"),
            {"mfa_secret": secret, "mfa_backup_codes": []},
        ])
        db.execute = AsyncMock()
        with pytest.raises(HTTPException) as exc:
            await mfa_verify(_req(), MFAVerifyBody(code="000000", mfa_challenge="tok"), db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_expired_challenge_returns_401(self):
        from routers.mfa import mfa_verify, MFAVerifyBody
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await mfa_verify(_req(), MFAVerifyBody(code="123456", mfa_challenge="expired"), db)
        assert exc.value.status_code == 401
        assert "expired" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_valid_backup_code_accepted(self):
        import pyotp
        from routers.mfa import mfa_verify, MFAVerifyBody, _hash_backup_code
        db = AsyncMock()
        secret = pyotp.random_base32()
        backup = "AAAA1111"
        hashed = [_hash_backup_code(backup)]
        db.fetchrow = AsyncMock(side_effect=[
            self._challenge("user"),
            {"mfa_secret": secret, "mfa_backup_codes": hashed},
        ])
        db.execute = AsyncMock()
        result = await mfa_verify(_req(), MFAVerifyBody(code=backup, mfa_challenge="tok"), db)
        assert result.get("mfa_verified") is True or result.get("success") is True

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_admin_challenge_returns_admin_token(self):
        import pyotp
        from routers.mfa import mfa_verify, MFAVerifyBody
        db = AsyncMock()
        secret = pyotp.random_base32()
        db.fetchrow = AsyncMock(side_effect=[
            self._challenge("admin", "uid-admin"),
            {"mfa_secret": secret, "mfa_backup_codes": []},
        ])
        db.execute = AsyncMock()
        result = await mfa_verify(_req(), MFAVerifyBody(code=pyotp.TOTP(secret).now(), mfa_challenge="tok"), db)
        assert result.get("token_type") == "admin"
        assert result.get("token") is not None

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_provider_challenge_returns_pvdr_token(self):
        import pyotp
        from routers.mfa import mfa_verify, MFAVerifyBody
        db = AsyncMock()
        secret = pyotp.random_base32()
        db.fetchrow = AsyncMock(side_effect=[
            self._challenge("provider", "uid-pvdr"),
            {"mfa_secret": secret, "mfa_backup_codes": []},
        ])
        db.execute = AsyncMock()
        result = await mfa_verify(_req(), MFAVerifyBody(code=pyotp.TOTP(secret).now(), mfa_challenge="tok"), db)
        assert result.get("token_type") == "provider"
        assert result.get("token", "").startswith("pvdr_")


# ─────────────────────────────────────────────────────────────────────────────
# TestMFADisable — requires TOTP + password
# ─────────────────────────────────────────────────────────────────────────────

class TestMFADisable:
    """POST /auth/mfa/disable — needs TOTP; also needs password for provider/admin."""

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_valid_code_disables_developer_mfa(self):
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        db = AsyncMock()
        db.execute = AsyncMock()
        secret = pyotp.random_base32()
        with patch("routers.mfa._resolve_caller", return_value=(
            "user", "uid-1", "dev@example.com", "developer",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": None},
        )):
            result = await mfa_disable(_req(), MFADisableBody(code=pyotp.TOTP(secret).now()), db)
        assert result["mfa_enabled"] is False

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_wrong_totp_returns_401(self):
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        with patch("routers.mfa._resolve_caller", return_value=(
            "user", "uid-1", "dev@example.com", "developer",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": None},
        )):
            with pytest.raises(HTTPException) as exc:
                await mfa_disable(_req(), MFADisableBody(code="000000"), db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_wrong_password_returns_401_for_provider(self):
        import bcrypt
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        pw_hash = bcrypt.hashpw(b"correct", bcrypt.gensalt()).decode()
        with patch("routers.mfa._resolve_caller", return_value=(
            "provider", "uid-1", "p@example.com", "provider",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": pw_hash},
        )):
            with pytest.raises(HTTPException) as exc:
                await mfa_disable(_req(), MFADisableBody(code=pyotp.TOTP(secret).now(), password="wrong"), db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_mfa_not_enabled_returns_400(self):
        from routers.mfa import mfa_disable, MFADisableBody
        from fastapi import HTTPException
        db = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            with pytest.raises(HTTPException) as exc:
                await mfa_disable(_req(), MFADisableBody(code="123456"), db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_provider_disable_requires_password(self):
        import bcrypt
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        from fastapi import HTTPException
        db = AsyncMock()
        secret = pyotp.random_base32()
        pw_hash = bcrypt.hashpw(b"mypassword", bcrypt.gensalt()).decode()
        with patch("routers.mfa._resolve_caller", return_value=(
            "provider", "uid-1", "p@example.com", "provider",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": pw_hash},
        )):
            with pytest.raises(HTTPException):
                await mfa_disable(_req(), MFADisableBody(code=pyotp.TOTP(secret).now(), password=""), db)

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_admin_disable_with_correct_credentials_succeeds(self):
        import bcrypt
        import pyotp
        from routers.mfa import mfa_disable, MFADisableBody
        db = AsyncMock()
        db.execute = AsyncMock()
        secret = pyotp.random_base32()
        pw_hash = bcrypt.hashpw(b"adminpass", bcrypt.gensalt()).decode()
        with patch("routers.mfa._resolve_caller", return_value=(
            "admin", "uid-admin", "admin@example.com", "admin",
            {"mfa_secret": secret, "mfa_enabled": True, "password_hash": pw_hash},
        )):
            result = await mfa_disable(_req(), MFADisableBody(code=pyotp.TOTP(secret).now(), password="adminpass"), db)
        assert result["success"] is True


# ─────────────────────────────────────────────────────────────────────────────
# TestMFAReset — admin-only lockout recovery
# ─────────────────────────────────────────────────────────────────────────────

_CEO_SESSION = {"role": "ceo", "email": "ceo@wayforth.io"}
_SUPPORT_SESSION = {"role": "support", "email": "support@wayforth.io"}
_ANALYTICS_SESSION = {"role": "analytics", "email": "analytics@wayforth.io"}


class TestMFAReset:
    """POST /auth/mfa/reset — admin clears MFA for any account type."""

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    @pytest.mark.parametrize("user_type", ["user", "provider", "admin"])
    async def test_ceo_can_reset_any_user_type(self, user_type):
        from routers.mfa import mfa_reset, MFAResetBody
        db = AsyncMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        with patch("routers.admin.dashboard.get_admin_session", new=AsyncMock(return_value=_CEO_SESSION)):
            result = await mfa_reset(
                _req(), MFAResetBody(user_id="00000000-0000-0000-0000-000000000001", user_type=user_type), db
            )
        assert result["success"] is True
        assert result["user_type"] == user_type

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_support_role_can_reset(self):
        from routers.mfa import mfa_reset, MFAResetBody
        db = AsyncMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        with patch("routers.admin.dashboard.get_admin_session", new=AsyncMock(return_value=_SUPPORT_SESSION)):
            result = await mfa_reset(
                _req(), MFAResetBody(user_id="00000000-0000-0000-0000-000000000001", user_type="user"), db
            )
        assert result["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_analytics_role_is_forbidden(self):
        from routers.mfa import mfa_reset, MFAResetBody
        from fastapi import HTTPException
        db = AsyncMock()
        with patch("routers.admin.dashboard.get_admin_session", new=AsyncMock(return_value=_ANALYTICS_SESSION)):
            with pytest.raises(HTTPException) as exc:
                await mfa_reset(
                    _req(), MFAResetBody(user_id="uid-1", user_type="user"), db
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_invalid_user_type_returns_400(self):
        from routers.mfa import mfa_reset, MFAResetBody
        from fastapi import HTTPException
        db = AsyncMock()
        with patch("routers.admin.dashboard.get_admin_session", new=AsyncMock(return_value=_CEO_SESSION)):
            with pytest.raises(HTTPException) as exc:
                await mfa_reset(_req(), MFAResetBody(user_id="uid-1", user_type="superuser"), db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_user_not_found_returns_404(self):
        from routers.mfa import mfa_reset, MFAResetBody
        from fastapi import HTTPException
        db = AsyncMock()
        db.execute = AsyncMock(return_value="UPDATE 0")
        with patch("routers.admin.dashboard.get_admin_session", new=AsyncMock(return_value=_CEO_SESSION)):
            with pytest.raises(HTTPException) as exc:
                await mfa_reset(
                    _req(), MFAResetBody(user_id="00000000-0000-0000-0000-000000000099", user_type="user"), db
                )
        assert exc.value.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# TestMFAStatus — authenticated status check
# ─────────────────────────────────────────────────────────────────────────────

class TestMFAStatus:
    """GET /auth/mfa/status — returns mfa_enabled, mfa_enabled_at, dashboard_type."""

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_returns_mfa_disabled_by_default(self):
        from routers.mfa import mfa_status
        db = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=_caller()):
            result = await mfa_status(_req(), db)
        assert result["mfa_enabled"] is False
        assert result["mfa_enabled_at"] is None

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_returns_mfa_enabled_true_when_set(self):
        from datetime import datetime, timezone
        from routers.mfa import mfa_status
        db = AsyncMock()
        enabled_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
        row = {"mfa_enabled": True, "mfa_enabled_at": enabled_at}
        with patch("routers.mfa._resolve_caller", return_value=(
            "user", "uid-1", "dev@example.com", "developer", row
        )):
            result = await mfa_status(_req(), db)
        assert result["mfa_enabled"] is True
        assert "2026" in result["mfa_enabled_at"]

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    @pytest.mark.parametrize("user_type,expected_dashboard", [
        ("user",     "developer"),
        ("provider", "provider"),
        ("admin",    "admin"),
    ])
    async def test_dashboard_type_matches_auth_method(self, user_type, expected_dashboard):
        from routers.mfa import mfa_status
        db = AsyncMock()
        with patch("routers.mfa._resolve_caller", return_value=(
            user_type, "uid-1", f"{user_type}@example.com", expected_dashboard,
            {"mfa_enabled": False, "mfa_enabled_at": None},
        )):
            result = await mfa_status(_req(), db)
        assert result["dashboard_type"] == expected_dashboard

    @pytest.mark.asyncio
    @pytest.mark.no_api_key
    async def test_unauthenticated_raises_401(self):
        from routers.mfa import _resolve_caller
        from fastapi import HTTPException
        db = AsyncMock()
        db.fetchrow = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await _resolve_caller(_req({}), db)
        assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# TestMFALoginFlow — live API smoke tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMFALoginFlow:
    """Live API smoke tests — check MFA endpoints exist and return expected codes."""

    @pytest_asyncio.fixture
    async def c(self):
        async with httpx.AsyncClient(
            base_url=BASE_URL, timeout=30.0, follow_redirects=True
        ) as client:
            yield client

    @pytest.mark.no_api_key
    async def test_status_without_auth_returns_401_or_404(self, c):
        r = await c.get("/auth/mfa/status")
        assert r.status_code in (401, 404)

    @pytest.mark.no_api_key
    async def test_setup_without_auth_returns_401_or_404(self, c):
        r = await c.post("/auth/mfa/setup")
        assert r.status_code in (401, 404)

    @pytest.mark.no_api_key
    async def test_verify_setup_without_auth_returns_401_or_404(self, c):
        r = await c.post("/auth/mfa/verify-setup", json={"code": "123456"})
        assert r.status_code in (401, 404, 422)

    @pytest.mark.no_api_key
    async def test_verify_bad_challenge_returns_401_or_404(self, c):
        r = await c.post("/auth/mfa/verify", json={"code": "123456", "mfa_challenge": "invalid"})
        assert r.status_code in (401, 404, 422)

    async def test_status_with_api_key_returns_mfa_fields(self, c):
        if not API_KEY:
            pytest.skip("WAYFORTH_TEST_API_KEY not set")
        r = await c.get("/auth/mfa/status", headers={"X-Wayforth-API-Key": API_KEY})
        if r.status_code == 404:
            pytest.skip("MFA endpoints not yet deployed")
        assert r.status_code == 200
        data = r.json()
        assert "mfa_enabled" in data
        assert "dashboard_type" in data

    async def test_mfa_disabled_initially_for_developer(self, c):
        if not API_KEY:
            pytest.skip("WAYFORTH_TEST_API_KEY not set")
        r = await c.get("/auth/mfa/status", headers={"X-Wayforth-API-Key": API_KEY})
        if r.status_code == 404:
            pytest.skip("MFA endpoints not yet deployed")
        assert r.json()["mfa_enabled"] is False

    async def test_developer_dashboard_type_from_api_key(self, c):
        if not API_KEY:
            pytest.skip("WAYFORTH_TEST_API_KEY not set")
        r = await c.get("/auth/mfa/status", headers={"X-Wayforth-API-Key": API_KEY})
        if r.status_code == 404:
            pytest.skip("MFA endpoints not yet deployed")
        assert r.json()["dashboard_type"] == "developer"
