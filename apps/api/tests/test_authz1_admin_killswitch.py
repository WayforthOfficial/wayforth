"""AUTHZ-1 regression — the static X-Admin-Key break-glass must be gated on
WAYFORTH_ADMIN_KEY_ENABLED (default off) and audit-logged. Before the fix, a
leaked key worked even with the switch off because most admin routers compared
the key directly instead of routing through the gated helper.

These are pure unit tests over core.admin_auth — no live gateway or DB needed.
Run: uv run pytest tests/test_authz1_admin_killswitch.py -v
"""
import sys
import types

import pytest

from core.admin_auth import admin_authed, admin_key_ok


class _URL:
    path = "/admin/revenue"


class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, headers):
        self.headers = headers
        self.url = _URL()
        self.client = _Client()


class _FakeDB:
    """Minimal asyncpg-connection stand-in: returns a fixed admin_sessions row."""
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_a, **_k):
        return self._row


@pytest.fixture(autouse=True)
def _stub_main(monkeypatch):
    # admin_key_ok does `from main import ADMIN_KEY` lazily; stub it so we don't
    # import the whole app just to read one constant.
    stub = types.ModuleType("main")
    stub.ADMIN_KEY = "test-admin-secret"
    monkeypatch.setitem(sys.modules, "main", stub)


def test_correct_key_is_inert_when_switch_unset(monkeypatch):
    monkeypatch.delenv("WAYFORTH_ADMIN_KEY_ENABLED", raising=False)
    assert admin_key_ok(_Req({"X-Admin-Key": "test-admin-secret"})) is False


def test_correct_key_is_inert_when_switch_false(monkeypatch):
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "false")
    assert admin_key_ok(_Req({"X-Admin-Key": "test-admin-secret"})) is False


def test_correct_key_works_only_when_switch_enabled(monkeypatch):
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "true")
    assert admin_key_ok(_Req({"X-Admin-Key": "test-admin-secret"})) is True


def test_wrong_key_rejected_even_when_enabled(monkeypatch):
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "true")
    assert admin_key_ok(_Req({"X-Admin-Key": "nope"})) is False


def test_no_key_rejected(monkeypatch):
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "true")
    assert admin_key_ok(_Req({})) is False


async def test_admin_authed_token_path_works_when_key_disabled(monkeypatch):
    # Disabling break-glass must NOT lock admins out: a valid session token
    # still authenticates via admin_authed.
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "false")
    db = _FakeDB({"expires_at": "future", "is_active": True})
    assert await admin_authed(_Req({"X-Admin-Token": "valid-token"}), db) is True


async def test_admin_authed_rejects_inactive_admin(monkeypatch):
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "false")
    db = _FakeDB({"expires_at": "future", "is_active": False})
    assert await admin_authed(_Req({"X-Admin-Token": "valid-token"}), db) is False


async def test_admin_authed_rejects_disabled_key_and_no_token(monkeypatch):
    monkeypatch.setenv("WAYFORTH_ADMIN_KEY_ENABLED", "false")
    db = _FakeDB(None)
    assert await admin_authed(_Req({"X-Admin-Key": "test-admin-secret"}), db) is False
