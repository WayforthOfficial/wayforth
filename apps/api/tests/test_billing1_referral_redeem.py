"""BILLING-1 regression — referral redemption must not double-grant.

Proves the atomic-claim logic in _redeem_in_tx: the 500-credit grant runs ONLY
on a successful, unique claim. A lost race (claim returns no row), an already
redeemed user, or a unique-index violation all 422 with NO grant.

Run: uv run pytest tests/test_billing1_referral_redeem.py -v
"""
import asyncpg
import pytest
from fastapi import HTTPException

from routers.billing.referrals import _redeem_in_tx

REFERRER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REDEEMER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CODE = "WF-ABC123"


class _FakeConn:
    """Scriptable asyncpg-connection stand-in. Records grant calls."""
    def __init__(self, *, referral_row, already, claim):
        self._referral_row = referral_row
        self._already = already
        self._claim = claim          # dict (claimed), None (race lost), or Exception
        self.grants = 0

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("SELECT referrer_user_id FROM referrals WHERE code"):
            return self._referral_row
        if q.startswith("SELECT 1 FROM referrals WHERE referred_user_id"):
            return self._already
        if q.startswith("UPDATE referrals SET referred_user_id"):
            if isinstance(self._claim, Exception):
                raise self._claim
            return self._claim
        raise AssertionError(f"unexpected query: {q}")

    async def execute(self, query, *args):
        if "UPDATE api_keys" in query:
            self.grants += 1
        return "UPDATE 1"


async def test_happy_path_grants_once():
    conn = _FakeConn(referral_row={"referrer_user_id": REFERRER}, already=None, claim={"id": 1})
    await _redeem_in_tx(conn, REDEEMER, CODE)
    assert conn.grants == 1


async def test_already_redeemed_no_grant():
    conn = _FakeConn(referral_row={"referrer_user_id": REFERRER}, already={"?": 1}, claim={"id": 1})
    with pytest.raises(HTTPException) as exc:
        await _redeem_in_tx(conn, REDEEMER, CODE)
    assert exc.value.status_code == 422
    assert conn.grants == 0


async def test_race_lost_claim_returns_none_no_grant():
    # Concurrent redeem already claimed this code → conditional UPDATE returns no row.
    conn = _FakeConn(referral_row={"referrer_user_id": REFERRER}, already=None, claim=None)
    with pytest.raises(HTTPException) as exc:
        await _redeem_in_tx(conn, REDEEMER, CODE)
    assert exc.value.status_code == 422
    assert conn.grants == 0


async def test_unique_violation_no_grant():
    # Same user claiming a second code concurrently → partial-unique-index violation.
    conn = _FakeConn(
        referral_row={"referrer_user_id": REFERRER},
        already=None,
        claim=asyncpg.UniqueViolationError("dup"),
    )
    with pytest.raises(HTTPException) as exc:
        await _redeem_in_tx(conn, REDEEMER, CODE)
    assert exc.value.status_code == 422
    assert conn.grants == 0


async def test_own_code_rejected():
    conn = _FakeConn(referral_row={"referrer_user_id": REDEEMER}, already=None, claim={"id": 1})
    with pytest.raises(HTTPException) as exc:
        await _redeem_in_tx(conn, REDEEMER, CODE)
    assert exc.value.status_code == 400
    assert conn.grants == 0


async def test_invalid_code():
    conn = _FakeConn(referral_row=None, already=None, claim={"id": 1})
    with pytest.raises(HTTPException) as exc:
        await _redeem_in_tx(conn, REDEEMER, CODE)
    assert exc.value.status_code == 404
    assert conn.grants == 0
