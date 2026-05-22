"""tests/test_provision_user_api_key.py — unit tests for the provisioning script.

The script is meant to be run by a human against production with a real Postgres,
so the tests focus on the dangerous-to-get-wrong parts:

  - Key generation matches the canonical wf_live_* + sha256 pattern used by
    routers/auth.py:register_user. If those drift, the new key would land in
    a row the rest of the codebase can't authenticate.

  - Existing active keys are deactivated UNLESS --keep-existing is passed —
    a subtle bug here would leave two valid keys per account and surprise
    the operator running the script.

  - Dry-run never writes (no INSERT / UPDATE issued).

  - When the user does not exist, the script returns exit code 2 and never
    inserts an api_keys row (would otherwise leave a dangling key).

A fake asyncpg.Connection lets us assert on the exact SQL emitted without
needing a database. The fake captures every execute()/fetchrow()/fetch()
call and dispatches by SQL substring.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# Fake asyncpg connection
# ─────────────────────────────────────────────────────────────────────────────


class _FakeTx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.calls.append(("BEGIN", None, None))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.conn.calls.append(("ROLLBACK", None, None))
            return False
        self.conn.calls.append(("COMMIT", None, None))
        return False


class FakeConn:
    """Capture-and-dispatch fake. `responses` maps SQL substring → return value."""

    def __init__(self, user_row=None, existing_keys=None):
        self.user_row = user_row
        self.existing_keys = existing_keys or []
        self.calls: list[tuple] = []          # (kind, sql, args)
        self.inserted_row: dict | None = None
        self.closed = False

    def transaction(self):
        return _FakeTx(self)

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        s = " ".join(sql.split())
        if "FROM users WHERE email" in s:
            return self.user_row
        if "FROM api_keys WHERE key_hash" in s and self.inserted_row:
            # Post-insert confirmation query.
            return self.inserted_row
        return None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        s = " ".join(sql.split())
        if "FROM api_keys WHERE user_id" in s:
            return self.existing_keys
        return []

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        s = " ".join(sql.split())
        if s.startswith("UPDATE api_keys SET active = FALSE"):
            return "UPDATE %d" % sum(1 for k in self.existing_keys if k["active"])
        if "INSERT INTO api_keys" in s:
            # Capture the insert as the row a follow-up SELECT would return.
            key_hash = args[0]
            self.inserted_row = {
                "id": "00000000-0000-0000-0000-000000000001",
                "key_prefix": args[1],
                "tier": args[2],
                "active": True,
                "monthly_quota": args[7],
                "rate_limit_per_minute": args[6],
                "created_at": "2026-05-22T00:00:00+00:00",
            }
            return "INSERT 0 1"
        return "OK"

    async def close(self):
        self.closed = True


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_script():
    """Import the script module. It lives outside the test path so we use
    importlib + the package's scripts/ directory."""
    spec = importlib.util.spec_from_file_location(
        "provision_user_api_key",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "provision_user_api_key.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _calls_of(conn: FakeConn, kind: str) -> list[tuple]:
    return [c for c in conn.calls if c[0] == kind]


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestKeyGeneration:

    def test_generated_key_matches_register_user_pattern(self):
        """The script and routers/auth.py:register_user MUST produce keys
        of the same shape — if they drift, the new key won't authenticate."""
        mod = _load_script()
        raw, key_hash, key_prefix = mod._generate_key()
        assert raw.startswith("wf_live_")
        # 8-char prefix + 32 bytes urlsafe-base64 → 8 + ~43 = ~51 chars total
        assert 48 <= len(raw) <= 60
        # key_hash is sha256(raw) in hex
        import hashlib as _h
        assert key_hash == _h.sha256(raw.encode()).hexdigest()
        # prefix is the first 12 chars (wf_live_ + first 4 of the random tail)
        assert key_prefix == raw[:12]
        assert len(key_prefix) == 12

    def test_consecutive_keys_are_distinct(self):
        mod = _load_script()
        a, _, _ = mod._generate_key()
        b, _, _ = mod._generate_key()
        assert a != b

    def test_maybe_encrypt_returns_none_without_env(self):
        mod = _load_script()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENCRYPTION_KEY", None)
            assert mod._maybe_encrypt("wf_live_anything") is None

    def test_maybe_encrypt_returns_fernet_blob_when_env_set(self):
        from cryptography.fernet import Fernet
        mod = _load_script()
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"ENCRYPTION_KEY": key}):
            blob = mod._maybe_encrypt("wf_live_secret")
        assert blob is not None
        # Round-trip — what we stored MUST decrypt back to the raw key.
        assert Fernet(key.encode()).decrypt(blob.encode()).decode() == "wf_live_secret"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end via the async `provision()` entry point.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestProvisionFlow:

    async def _run(self, conn: FakeConn, **kwargs):
        mod = _load_script()

        async def _fake_connect(_url):
            return conn

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://stub"}), \
             patch("asyncpg.connect", side_effect=_fake_connect):
            return await mod.provision(
                email=kwargs.get("email", "demo_growth@wayforth.io"),
                tier=kwargs.get("tier", "growth"),
                keep_existing=kwargs.get("keep_existing", False),
                dry_run=kwargs.get("dry_run", False),
            )

    async def test_user_not_found_returns_2_and_writes_nothing(self):
        conn = FakeConn(user_row=None)
        rc = await self._run(conn)
        assert rc == 2
        assert not _calls_of(conn, "execute"), \
            "must not write when user does not exist"

    async def test_dry_run_writes_nothing(self):
        conn = FakeConn(
            user_row={"id": "u1", "email": "demo_growth@wayforth.io"},
            existing_keys=[],
        )
        rc = await self._run(conn, dry_run=True)
        assert rc == 0
        assert not _calls_of(conn, "execute")

    async def test_happy_path_inserts_active_key_with_tier_defaults(self):
        conn = FakeConn(
            user_row={"id": "u1", "email": "demo_growth@wayforth.io"},
            existing_keys=[],
        )
        rc = await self._run(conn, tier="growth")
        assert rc == 0
        # Exactly one INSERT INTO api_keys.
        inserts = [c for c in _calls_of(conn, "execute")
                   if "INSERT INTO api_keys" in _normalize(c[1])]
        assert len(inserts) == 1
        _, _, args = inserts[0]
        # Args: key_hash, key_prefix, tier, user_id, owner_email,
        #       encrypted_key, rpm, monthly_quota
        assert args[2] == "growth"
        assert args[3] == "u1"
        assert args[4] == "demo_growth@wayforth.io"
        assert args[6] == 300         # growth rpm
        assert args[7] == 500_000     # growth monthly_quota
        # And the key_hash is the sha256 of a wf_live_* token.
        assert len(args[0]) == 64     # sha256 hex
        assert args[1].startswith("wf_live_")

    async def test_deactivates_existing_active_keys_by_default(self):
        conn = FakeConn(
            user_row={"id": "u1", "email": "demo_growth@wayforth.io"},
            existing_keys=[
                {"id": "k1", "key_prefix": "wf_live_old1", "active": True,
                 "created_at": "2025-01-01"},
                {"id": "k2", "key_prefix": "wf_live_old2", "active": False,
                 "created_at": "2024-01-01"},
            ],
        )
        rc = await self._run(conn)
        assert rc == 0
        deactivations = [c for c in _calls_of(conn, "execute")
                         if "UPDATE api_keys SET active = FALSE" in _normalize(c[1])]
        assert len(deactivations) == 1, \
            "must deactivate existing active keys before inserting the new one"

    async def test_keep_existing_skips_deactivation(self):
        conn = FakeConn(
            user_row={"id": "u1", "email": "demo_growth@wayforth.io"},
            existing_keys=[
                {"id": "k1", "key_prefix": "wf_live_old1", "active": True,
                 "created_at": "2025-01-01"},
            ],
        )
        rc = await self._run(conn, keep_existing=True)
        assert rc == 0
        deactivations = [c for c in _calls_of(conn, "execute")
                         if "UPDATE api_keys SET active = FALSE" in _normalize(c[1])]
        assert len(deactivations) == 0


