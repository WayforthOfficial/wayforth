"""tests/test_v080_security.py — Unit tests for v0.8.0 pre-mainnet security fixes.

Self-contained. No live deployment, no real DB, no real Redis. Each test
section maps to one item in the v0.8.0 security release:

  - Item 1: x402 payment replay protection (durable, UNIQUE(payment_hash))
  - Item 2: provider email verification
  - Item 3: API key encryption versioning
  - Item 4: append-only admin audit log
  - Item 5: WRI alert retry queue (reuse webhook_deliveries with kind column)

Run: pytest apps/api/tests/test_v080_security.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# Item 1: x402 payment replay protection
# ─────────────────────────────────────────────────────────────────────────────
#
# The security property: two requests bearing the same X-PAYMENT header must
# never result in two service executions. This is enforced by
# UNIQUE(payment_hash) on x402_settlements; the application translates
# asyncpg.UniqueViolationError into a 400 replay_rejected response.


class _SettlementsTable:
    """Minimal asyncpg-shaped stub that emulates a UNIQUE(payment_hash) row."""

    def __init__(self):
        self.rows: dict[str, dict] = {}  # payment_hash → row

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO x402_settlements"):
            payment_hash, amount, service_slug, user_id = args
            if payment_hash in self.rows:
                # asyncpg raises UniqueViolationError on UNIQUE constraint hit.
                raise asyncpg.UniqueViolationError(
                    "duplicate key value violates unique constraint "
                    "\"x402_settlements_payment_hash_unique\""
                )
            self.rows[payment_hash] = {
                "payment_hash": payment_hash,
                "amount": amount,
                "service_slug": service_slug,
                "user_id": user_id,
            }
            return
        raise NotImplementedError(f"_SettlementsTable does not implement: {q[:60]}")


@pytest.mark.asyncio
async def test_x402_settlement_unique_constraint_emulation():
    """Sanity-check: the stub raises UniqueViolationError on a repeated hash.
    The real DB enforces this with the UNIQUE constraint declared in
    infra/migrations/040_x402_settlements_retroactive.sql (and the v0.7.8
    lifespan create in main.py).
    """
    tbl = _SettlementsTable()
    await tbl.execute(
        "INSERT INTO x402_settlements (payment_hash, amount, service_slug, user_id) "
        "VALUES ($1, $2, $3, $4)",
        "hash-abc", 0.01, "deepl", None,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await tbl.execute(
            "INSERT INTO x402_settlements (payment_hash, amount, service_slug, user_id) "
            "VALUES ($1, $2, $3, $4)",
            "hash-abc", 0.01, "deepl", None,
        )


@pytest.mark.asyncio
async def test_x402_replay_distinct_hashes_both_succeed():
    """Two different X-PAYMENT headers (different EIP-3009 nonces) must both
    settle independently. This guards against an over-zealous dedup that
    would block legitimate sequential payments from the same wallet."""
    tbl = _SettlementsTable()
    for h in ("hash-1", "hash-2", "hash-3"):
        await tbl.execute(
            "INSERT INTO x402_settlements (payment_hash, amount, service_slug, user_id) "
            "VALUES ($1, $2, $3, $4)",
            h, 0.01, "deepl", None,
        )
    assert len(tbl.rows) == 3


@pytest.mark.asyncio
async def test_x402_concurrent_replay_only_one_wins():
    """Concurrent identical requests must serialise: exactly one INSERT
    succeeds, the other raises UniqueViolationError. Models the
    same-X-PAYMENT-two-workers race that the v0.7.8 in-memory dict couldn't
    cover and the v0.8.0 UNIQUE constraint does."""
    tbl = _SettlementsTable()
    payment_hash = "hash-race"

    async def _try_insert():
        try:
            await tbl.execute(
                "INSERT INTO x402_settlements "
                "(payment_hash, amount, service_slug, user_id) "
                "VALUES ($1, $2, $3, $4)",
                payment_hash, 0.01, "deepl", None,
            )
            return "ok"
        except asyncpg.UniqueViolationError:
            return "replay_rejected"

    results = await asyncio.gather(_try_insert(), _try_insert(), _try_insert())
    assert results.count("ok") == 1
    assert results.count("replay_rejected") == 2


def test_x402_router_catches_unique_violation():
    """The x402 router must catch asyncpg.UniqueViolationError and return a
    400 replay_rejected JSON body — not bubble a 500 to the caller.

    This is a static assertion against the source so a regression that
    accidentally drops the except clause is caught at test time.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "routers" / "x402.py"
    text = src.read_text()
    # Both x402_execute and x402_search must INSERT into x402_settlements
    # and must catch UniqueViolationError.
    assert text.count("INSERT INTO x402_settlements") >= 2, (
        "v0.8.0 Item 1: expected at least two INSERTs into x402_settlements "
        "(x402_execute and x402_search). Found fewer — a settlement path is "
        "missing replay protection."
    )
    assert text.count("asyncpg.UniqueViolationError") >= 2, (
        "v0.8.0 Item 1: expected at least two except asyncpg.UniqueViolationError "
        "clauses in routers/x402.py. A missing handler would leak a 500 to a "
        "replaying caller."
    )
    assert "replay_rejected" in text, (
        "v0.8.0 Item 1: response must use the 'replay_rejected' error code so "
        "clients can detect this case specifically."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Item 2: provider email verification
# ─────────────────────────────────────────────────────────────────────────────


from datetime import datetime, timedelta, timezone


class _ProvidersDB:
    """Minimal asyncpg-shaped fake for the providers table."""

    def __init__(self):
        self.providers: dict[str, dict] = {}  # id → row
        self._next_id = 1

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT id FROM providers WHERE email"):
            for p in self.providers.values():
                if p["email"] == args[0]:
                    return p["id"]
            return None
        if "INSERT INTO providers" in q and "RETURNING id" in q:
            pid = f"pvdr-{self._next_id}"
            self._next_id += 1
            self.providers[pid] = {
                "id": pid,
                "company_name": args[0],
                "email": args[1],
                "password_hash": args[2],
                "email_verification_token": args[3] if len(args) > 3 else None,
                "email_verification_sent_at": datetime.now(timezone.utc) if len(args) > 3 else None,
                "email_verified": False,
            }
            return pid
        return None

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if "FROM providers WHERE email_verification_token" in q:
            for p in self.providers.values():
                if p.get("email_verification_token") == args[0]:
                    return p
            return None
        if "FROM providers WHERE email" in q:
            for p in self.providers.values():
                if p["email"] == args[0]:
                    return p
            return None
        return None

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if "UPDATE providers SET email_verified = true" in q:
            pid = args[0]
            if pid in self.providers:
                self.providers[pid]["email_verified"] = True
                self.providers[pid]["email_verification_token"] = None
            return
        if "UPDATE providers" in q and "email_verification_token" in q and "email_verification_sent_at" in q:
            new_token, pid = args
            if pid in self.providers:
                self.providers[pid]["email_verification_token"] = new_token
                self.providers[pid]["email_verification_sent_at"] = datetime.now(timezone.utc)
            return
        if "INSERT INTO provider_services" in q:
            return  # ignore — not under test here


@pytest.mark.asyncio
async def test_provider_verify_email_valid_token_marks_verified():
    """Hitting GET /provider/verify-email with a valid token flips the flag."""
    from routers.provider import provider_verify_email
    db = _ProvidersDB()
    # Pre-seed an unverified provider.
    db.providers["p1"] = {
        "id": "p1",
        "email": "alice@example.com",
        "email_verification_token": "fresh-token-abc-1234567890-xyz",
        "email_verification_sent_at": datetime.now(timezone.utc),
        "email_verified": False,
        "company_name": "Acme",
    }
    result = await provider_verify_email("fresh-token-abc-1234567890-xyz", db)
    assert result == {"email_verified": True, "message": "Email verified. You can now use provider write endpoints."}
    assert db.providers["p1"]["email_verified"] is True
    assert db.providers["p1"]["email_verification_token"] is None


@pytest.mark.asyncio
async def test_provider_verify_email_expired_token_returns_410():
    """Tokens older than 24h are rejected with 410 Gone."""
    from fastapi import HTTPException
    from routers.provider import provider_verify_email
    db = _ProvidersDB()
    db.providers["p1"] = {
        "id": "p1",
        "email": "alice@example.com",
        "email_verification_token": "stale-token-abcdefgh-1234567890",
        "email_verification_sent_at": datetime.now(timezone.utc) - timedelta(hours=25),
        "email_verified": False,
    }
    with pytest.raises(HTTPException) as exc_info:
        await provider_verify_email("stale-token-abcdefgh-1234567890", db)
    assert exc_info.value.status_code == 410
    assert exc_info.value.detail["error"] == "token_expired"
    # And the flag must still be false.
    assert db.providers["p1"]["email_verified"] is False


@pytest.mark.asyncio
async def test_provider_verify_email_unknown_token_returns_404():
    """An unknown token must not leak whether any provider matched."""
    from fastapi import HTTPException
    from routers.provider import provider_verify_email
    db = _ProvidersDB()
    with pytest.raises(HTTPException) as exc_info:
        await provider_verify_email("totally-bogus-token-1234567890", db)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_provider_verify_email_idempotent_on_already_verified():
    """A second click on the verification link must not error — return success."""
    from routers.provider import provider_verify_email
    db = _ProvidersDB()
    db.providers["p1"] = {
        "id": "p1",
        "email": "alice@example.com",
        "email_verification_token": "leftover-token-abcdefgh1234567890",
        "email_verification_sent_at": datetime.now(timezone.utc),
        "email_verified": True,  # already verified
    }
    result = await provider_verify_email("leftover-token-abcdefgh1234567890", db)
    assert result["email_verified"] is True
    assert "already verified" in result["message"].lower()


@pytest.mark.asyncio
async def test_require_email_verified_blocks_unverified_provider():
    """The gate dependency must raise 403 when email_verified is false,
    even if the session token resolves a valid provider row."""
    from fastapi import HTTPException
    from routers.provider import _require_email_verified

    class FakeRequest:
        headers = {"X-Provider-Token": "valid-token-1234"}

    # Stub _get_provider via a fake DB that returns an unverified row.
    import hashlib as _h
    class _Db:
        async def fetchrow(self, q, *args):
            return {
                "provider_id": "p1",
                "company_name": "Acme",
                "email": "alice@example.com",
                "tier": "observer",
                "verified": False,
                "email_verified": False,
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
            }

    with pytest.raises(HTTPException) as exc_info:
        await _require_email_verified(FakeRequest(), _Db())
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"] == "email_not_verified"


@pytest.mark.asyncio
async def test_require_email_verified_passes_verified_provider():
    """When email_verified is true, the dependency returns the provider row."""
    from routers.provider import _require_email_verified

    class FakeRequest:
        headers = {"X-Provider-Token": "valid-token-1234"}

    class _Db:
        async def fetchrow(self, q, *args):
            return {
                "provider_id": "p1",
                "company_name": "Acme",
                "email": "alice@example.com",
                "tier": "observer",
                "verified": False,
                "email_verified": True,
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
            }

    provider = await _require_email_verified(FakeRequest(), _Db())
    assert provider["provider_id"] == "p1"
    assert provider["email_verified"] is True


def test_provider_write_endpoints_use_email_verified_gate():
    """Static check: provider.py's write endpoints must wire through
    _require_email_verified, not _get_provider directly."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "routers" / "provider.py"
    text = src.read_text()
    # /provider/verify and /provider/billing/upgrade must call _require_email_verified.
    # Count occurrences — should be at least 2 (one per gated endpoint).
    assert text.count("_require_email_verified(request, db)") >= 2, (
        "v0.8.0 Item 2: expected at least 2 calls to _require_email_verified "
        "in routers/provider.py (provider_verify + provider_billing_upgrade). "
        "A regression here would let unverified providers claim domains or "
        "initiate payments."
    )
