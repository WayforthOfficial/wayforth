"""tests/test_founding_bonus.py — Unit tests for _maybe_grant_founding_bonus.

Tests run without a live service; they mock the asyncpg connection so no
DB or network is required. All three scenarios are covered:
  1. Founding member on first invoice  → bonus granted (credits +500, timestamp set)
  2. Founding member on second invoice → no-op (founding_bonus_granted_at already set)
  3. Non-founding member               → no-op (founding_member = False)
"""

import asyncio
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.credits import _maybe_grant_founding_bonus


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_db(founding_member: bool, bonus_granted_at=None):
    """Return a minimal asyncpg-like connection mock."""
    db = AsyncMock()

    user_row = MagicMock()
    user_row.__bool__ = lambda self: True
    user_row.__getitem__ = lambda self, key: {
        "founding_member": founding_member,
        "founding_bonus_granted_at": bonus_granted_at,
    }[key]

    db.fetchrow = AsyncMock(return_value=user_row)
    db.execute = AsyncMock(return_value=None)

    # transaction() context manager that just executes the body
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    db.transaction = MagicMock(return_value=tx_cm)

    return db


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_founding_member_first_invoice_grants_bonus():
    """founding_member=True, no prior grant → 500 credits added, timestamp stamped."""
    db = _make_db(founding_member=True, bonus_granted_at=None)
    user_id = "00000000-0000-0000-0000-000000000001"

    with patch("core.credits._dispatch_webhooks", new_callable=AsyncMock) as mock_dispatch, \
         patch("asyncio.create_task") as mock_task:

        result = await _maybe_grant_founding_bonus(db, user_id)

    assert result is True

    # credits UPDATE must have been called
    update_calls = [str(c) for c in db.execute.call_args_list]
    assert any("credits_balance" in c for c in update_calls), \
        "Expected credits_balance UPDATE"
    assert any("founding_bonus_granted_at" in c for c in update_calls), \
        "Expected founding_bonus_granted_at UPDATE"

    # webhook task must be scheduled
    mock_task.assert_called_once()


@pytest.mark.asyncio
async def test_founding_member_second_invoice_no_bonus():
    """founding_member=True, bonus already granted → no-op."""
    from datetime import datetime, timezone
    already_granted = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = _make_db(founding_member=True, bonus_granted_at=already_granted)
    user_id = "00000000-0000-0000-0000-000000000002"

    with patch("asyncio.create_task") as mock_task:
        result = await _maybe_grant_founding_bonus(db, user_id)

    assert result is False
    db.execute.assert_not_called()
    mock_task.assert_not_called()


@pytest.mark.asyncio
async def test_non_founding_member_no_bonus():
    """founding_member=False → no-op regardless of grant timestamp."""
    db = _make_db(founding_member=False, bonus_granted_at=None)
    user_id = "00000000-0000-0000-0000-000000000003"

    with patch("asyncio.create_task") as mock_task:
        result = await _maybe_grant_founding_bonus(db, user_id)

    assert result is False
    db.execute.assert_not_called()
    mock_task.assert_not_called()


@pytest.mark.asyncio
async def test_user_not_found_no_bonus():
    """User row missing (e.g. lookup returns None) → no-op, no exception."""
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=None)

    with patch("asyncio.create_task") as mock_task:
        result = await _maybe_grant_founding_bonus(db, "nonexistent-uuid")

    assert result is False
    db.execute.assert_not_called()
    mock_task.assert_not_called()
