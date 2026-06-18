"""CLOUD-2 regression — credit_cap enforced as a live per-run ceiling.

check_run_credit_cap() refuses an agent-driven call when the run's cumulative
spend would exceed its cap, and no-ops for all non-cloud traffic.

Run: uv run pytest tests/test_cloud2_run_cap.py -v
"""
import pytest
from fastapi import HTTPException

from core.credits import check_run_credit_cap

RUN = "123e4567-e89b-12d3-a456-426614174000"


class _FakeDB:
    def __init__(self, run_row, spent):
        self._run = run_row
        self._spent = spent

    async def fetchrow(self, _q, *_a):
        return self._run

    async def fetchval(self, _q, *_a):
        return self._spent


async def test_non_cloud_string_id_is_noop():
    # Ordinary proxy traffic (agent_id not a UUID) → never queried, allowed.
    await check_run_credit_cap(_FakeDB({"credits_reserved": 100, "status": "running"}, 0),
                               "my-agent", 50)


async def test_under_cap_allowed():
    await check_run_credit_cap(_FakeDB({"credits_reserved": 100, "status": "running"}, 40),
                               RUN, 50)


async def test_over_cap_blocked():
    with pytest.raises(HTTPException) as exc:
        await check_run_credit_cap(_FakeDB({"credits_reserved": 100, "status": "running"}, 70),
                                   RUN, 50)
    assert exc.value.status_code == 402
    assert exc.value.detail["error"] == "agent_credit_cap_exceeded"


async def test_exactly_at_cap_allowed():
    await check_run_credit_cap(_FakeDB({"credits_reserved": 100, "status": "running"}, 70),
                               RUN, 30)


async def test_uncapped_run_allowed():
    await check_run_credit_cap(_FakeDB({"credits_reserved": 0, "status": "running"}, 9999),
                               RUN, 50)


async def test_finished_run_is_noop():
    await check_run_credit_cap(_FakeDB({"credits_reserved": 100, "status": "completed"}, 999),
                               RUN, 50)


async def test_no_agent_id_is_noop():
    await check_run_credit_cap(_FakeDB(None, 0), None, 50)
