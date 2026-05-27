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
