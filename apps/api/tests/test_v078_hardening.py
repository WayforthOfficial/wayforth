"""tests/test_v078_hardening.py — Unit tests for v0.7.8 hardening changes.

Self-contained. No live deployment, no real DB, no real Redis. Uses a tiny
in-memory fake DB that emulates the asyncpg interface (fetchval, fetchrow,
execute, transaction) just enough to exercise the two critical correctness
properties:

  - S1: stripe_webhook dedup must roll back atomically with the credit grant.
        A crash mid-processing leaves no dedup row, so the Stripe retry can
        reprocess.

  - E8: _do_refund must be idempotent when given a stable
        refund_idempotency_key. A second call with the same key returns the
        existing balance and does NOT issue a second credit increase.

Run: pytest apps/api/tests/test_v078_hardening.py -v
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────────────────────────────────────────────────────────
# Tiny in-memory fake DB
# ─────────────────────────────────────────────────────────────────────────────

class FakeDB:
    """asyncpg-shaped fake. Tracks a stripe_events set, a per-user credits
    balance, and an append-only credit_transactions log. Transactions are
    modeled as commit-or-rollback snapshots so we can verify atomicity."""

    def __init__(self):
        self.stripe_events: set[str] = set()
        self.user_credits: dict[str, int] = {}  # user_id -> credits_balance
        self.lifetime_credits: dict[str, int] = {}
        self.credit_tx: list[dict] = []  # rows
        self.api_keys: list[dict] = []
        self.package_purchases: dict[str, str] = {}  # session_id -> status
        # Transaction snapshot stack
        self._snapshots: list[tuple] = []
        # `raise_after` lets a test inject a crash mid-transaction
        self.raise_after: tuple[str, int] | None = None
        self._calls: list[str] = []

    def _snapshot(self):
        return (
            set(self.stripe_events),
            dict(self.user_credits),
            dict(self.lifetime_credits),
            list(self.credit_tx),
            list(self.api_keys),
            dict(self.package_purchases),
        )

    def _restore(self, snap):
        (
            self.stripe_events,
            self.user_credits,
            self.lifetime_credits,
            self.credit_tx,
            self.api_keys,
            self.package_purchases,
        ) = (set(snap[0]), dict(snap[1]), dict(snap[2]), list(snap[3]),
             list(snap[4]), dict(snap[5]))

    def transaction(self):
        outer = self

        @asynccontextmanager
        async def _txn():
            snap = outer._snapshot()
            try:
                yield
            except Exception:
                outer._restore(snap)
                raise
        return _txn()

    async def fetchval(self, query: str, *args):
        self._calls.append(query.strip().splitlines()[0])
        q = " ".join(query.split())
        # Stripe dedup INSERT
        if "INSERT INTO stripe_events" in q:
            event_id = args[0]
            if event_id in self.stripe_events:
                return None
            self.stripe_events.add(event_id)
            self._maybe_raise("stripe_events", len(self.stripe_events))
            return event_id
        if "SELECT id FROM package_purchases" in q:
            sid = args[0]
            return "row-1" if self.package_purchases.get(sid) == "completed" else None
        if "SELECT balance_after FROM credit_transactions" in q:
            refund_uuid = args[0]
            for tx in self.credit_tx:
                if tx.get("refund_uuid") == refund_uuid:
                    return tx["balance_after"]
            return None
        if "SELECT COUNT" in q:
            return 0
        return None

    async def fetchrow(self, query: str, *args):
        self._calls.append(query.strip().splitlines()[0])
        q = " ".join(query.split())
        if "UPDATE user_credits SET credits_balance" in q and "RETURNING credits_balance" in q:
            credit_cost, user_id = args[0], args[1]
            new = self.user_credits.get(user_id, 0) + credit_cost
            self.user_credits[user_id] = new
            self._maybe_raise("user_credits_update", new)
            return {"credits_balance": new}
        if "SELECT credits_balance FROM user_credits" in q:
            user_id = args[0]
            bal = self.user_credits.get(user_id)
            if bal is None:
                return None
            return {"credits_balance": bal}
        return None

    async def execute(self, query: str, *args):
        self._calls.append(query.strip().splitlines()[0])
        q = " ".join(query.split())
        if "INSERT INTO credit_transactions" in q:
            row = {
                "user_id": args[0], "amount": args[1], "balance_after": args[2],
                "type": "refund" if "refund" in q.lower() else "purchase",
                "description": args[3] if len(args) > 3 else "",
            }
            # _do_refund passes refund_uuid as last positional arg
            if "refund_uuid" in q:
                row["refund_uuid"] = args[-1]
            self.credit_tx.append(row)
            self._maybe_raise("credit_tx_insert", len(self.credit_tx))
            return
        if "UPDATE package_purchases SET payment_status" in q:
            self.package_purchases[args[0]] = "completed"
            return
        if "UPDATE user_credits" in q:
            credits_balance, _credits_added, _pkg, user_id = args[0], args[1], args[2], args[3]
            self.user_credits[user_id] = credits_balance
            self._maybe_raise("user_credits_update", credits_balance)
            return
        if "INSERT INTO user_credits" in q:
            user_id, credits, _pkg = args[0], args[1], args[2]
            self.user_credits[user_id] = credits
            self._maybe_raise("user_credits_insert", credits)
            return
        if "UPDATE api_keys" in q:
            return

    def _maybe_raise(self, marker: str, n: int):
        if self.raise_after and self.raise_after == (marker, n):
            raise RuntimeError(f"injected failure at {marker}#{n}")


# ─────────────────────────────────────────────────────────────────────────────
# S1: webhook dedup atomicity
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stripe_dedup_rolls_back_on_credit_grant_failure():
    """S1: if credit-grant logic raises after the dedup INSERT, the dedup row
    must roll back so the Stripe retry can reprocess."""
    from routers.billing.stripe import _process_stripe_event

    db = FakeDB()
    event = {
        "id": "evt_test_1",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_1",
            "metadata": {"user_id": "user-1", "package": "starter", "credits": 1000},
            "subscription": None,
        }},
    }

    # Inject a crash AFTER the credit_transactions INSERT (mid-transaction).
    # The user has no existing user_credits row so the code path hits
    # INSERT INTO user_credits, then INSERT INTO credit_transactions; we
    # crash on the second so credits are visible in our snapshot but the
    # rollback must wipe them along with the dedup row.
    db.raise_after = ("credit_tx_insert", 1)

    with pytest.raises(RuntimeError):
        await _process_stripe_event(db, event, "evt_test_1", "checkout.session.completed")

    # The dedup row must be gone (rolled back), so a retry can reprocess.
    assert "evt_test_1" not in db.stripe_events, \
        "S1 FAIL: dedup row persisted after a failed credit grant. Stripe retry would be silently dropped."
    # And the credit balance must NOT be incremented.
    assert db.user_credits.get("user-1", 0) == 0, \
        "S1 FAIL: credits incremented even though the txn was supposed to roll back."


@pytest.mark.asyncio
async def test_stripe_dedup_duplicate_returns_early_without_re_crediting():
    """A second delivery of the same event must return duplicate_event without
    re-running the credit grant."""
    from routers.billing.stripe import _process_stripe_event

    db = FakeDB()
    event = {
        "id": "evt_test_2",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test_2",
            "metadata": {"user_id": "user-2", "package": "starter", "credits": 1000},
            "subscription": None,
        }},
    }
    # First delivery
    r1 = await _process_stripe_event(db, event, "evt_test_2", "checkout.session.completed")
    assert r1.get("status") == "credited"
    first_bal = db.user_credits["user-2"]

    # Second delivery (same event) — must short-circuit.
    r2 = await _process_stripe_event(db, event, "evt_test_2", "checkout.session.completed")
    assert r2.get("status") == "duplicate_event", \
        f"S1 FAIL: second delivery did not short-circuit; got {r2}"
    # Balance unchanged.
    assert db.user_credits["user-2"] == first_bal, \
        "S1 FAIL: duplicate delivery double-credited the user."


# ─────────────────────────────────────────────────────────────────────────────
# E8: refund idempotency
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refund_idempotency_key_prevents_double_refund():
    """E8: calling _do_refund twice with the same refund_idempotency_key must
    return the same balance and only issue one credit increase."""
    from routers.execute import _do_refund

    db = FakeDB()
    # Seed: user has 500 credits.
    db.user_credits["user-3"] = 500

    refund_uuid = "11111111-1111-1111-1111-111111111111"
    first = await _do_refund(
        db, user_id="user-3", credit_cost=100,
        service_slug="some-svc", error_msg="upstream timeout",
        endpoint="/execute", balance_after=400,
        refund_idempotency_key=refund_uuid,
    )
    second = await _do_refund(
        db, user_id="user-3", credit_cost=100,
        service_slug="some-svc", error_msg="upstream timeout",
        endpoint="/execute", balance_after=400,
        refund_idempotency_key=refund_uuid,
    )

    assert first == 600, f"E8 FAIL: first refund returned {first}, expected 600"
    assert second == 600, f"E8 FAIL: idempotent second refund returned {second}, expected 600"
    # Balance must be 600, NOT 700.
    assert db.user_credits["user-3"] == 600, \
        f"E8 FAIL: double refund occurred. Balance={db.user_credits['user-3']}, expected 600."
    # Only one refund row should exist in credit_transactions.
    refund_rows = [tx for tx in db.credit_tx if tx.get("refund_uuid") == refund_uuid]
    assert len(refund_rows) == 1, \
        f"E8 FAIL: expected 1 refund row, got {len(refund_rows)}."


@pytest.mark.asyncio
async def test_refund_without_idempotency_key_still_works():
    """Backwards-compat: callers that don't supply a key get the historical
    behavior (no idempotency check)."""
    from routers.execute import _do_refund

    db = FakeDB()
    db.user_credits["user-4"] = 200

    # Suppress the dispatched-webhook side-effect since main.app isn't real.
    with patch("routers.execute.asyncio.create_task"):
        bal1 = await _do_refund(db, "user-4", 50, "svc", "err", "/execute", 150)
        bal2 = await _do_refund(db, "user-4", 50, "svc", "err", "/execute", 200)

    assert bal1 == 250
    assert bal2 == 300
    assert db.user_credits["user-4"] == 300
