"""test_cloud_reserve_release.py — money-path guard for the cloud-run reserve.

The reserve is a pure HOLD: proxy + compute are charged live to the balance during a
run, so the hold must be released IN FULL at completion. The net balance impact of a
reserved run must therefore equal actual_spend (proxy + compute) — never 2×.

This guards the fix for the double-charge where credits_released was
max(0, reserved - actual_spend): that left `actual_spend` of the hold unreturned on
TOP of the live charges, billing the real cost twice. It must fail against that old
behavior and pass against the full-release fix.
"""
from __future__ import annotations

import asyncio

import routers.cloud as cloud


def test_reserved_cloud_run_nets_to_actual_spend(monkeypatch):
    RESERVED = 50   # the hold deducted at dispatch (credit_cap)
    PROXY = 4       # live /proxy spend during the run (e.g. alphavantage)
    COMPUTE = 1     # compute charged at completion
    # Correct net balance impact for the whole run = -(PROXY + COMPUTE) = -5, NOT -10.

    released_calls: list[int] = []
    deduct_calls: list[int] = []
    statuses: list[str] = []

    class _Conn:
        async def fetchrow(self, q, *a):
            if "FROM hosted_agents" in q:           # code lookup → non-empty → happy path
                return {"code": "print('hi')"}
            return None

        async def execute(self, q, *a):
            if "UPDATE agent_runs SET status" in q:  # _update(status, …) — capture the status
                statuses.append(a[0])
            return None

    class _PoolCtx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *e):
            return False

    class _Pool:
        def acquire(self):
            return _PoolCtx()

    class _Result:
        stdout = "ok"; stderr = ""; exit_code = 0; duration_ms = 1000; sandbox_id = "sb-1"

    class _Provider:
        async def run(self, **kw):
            return _Result()

    async def fake_deduct(conn, user_id, amount, endpoint, **kw):
        deduct_calls.append(amount)                  # the live compute charge
        return True, 1000 - amount

    async def fake_release(pool, user_id, amount, run_id):
        released_calls.append(amount)                # how much of the hold is returned

    async def fake_signals(pool, run_id, user_id):
        return {"credits_proxy": PROXY, "services_called": [],
                "failover_events": 0, "substitutions": []}

    monkeypatch.setattr(cloud, "get_provider", lambda *a, **k: _Provider())
    monkeypatch.setattr(cloud, "compute_credits_for_run", lambda ms: COMPUTE)
    monkeypatch.setattr(cloud, "check_and_deduct_credits", fake_deduct)
    monkeypatch.setattr(cloud, "_release_reserve", fake_release)
    monkeypatch.setattr(cloud, "_reconcile_run_signals", fake_signals)

    agent = {"id": "11111111-1111-4111-8111-111111111111", "runtime": "python",
             "env_encrypted": None, "sandbox_provider": "e2b"}

    asyncio.run(cloud._execute_run(
        _Pool(), "22222222-2222-4222-8222-222222222222", agent,
        "user-1", "wf_live_test", RESERVED))

    # Guard we exercised the COMPLETION path (not an error branch that also full-releases).
    assert "completed" in statuses and "failed" not in statuses, f"statuses={statuses}"
    assert deduct_calls == [COMPUTE]

    # The fix: the hold is released IN FULL.
    assert released_calls == [RESERVED], (
        f"reserve must release in full (={RESERVED}); got {released_calls} — "
        "partial release double-charges proxy+compute")

    # The invariant: net balance impact over the whole run == actual_spend (no 2×).
    actual_spend = PROXY + COMPUTE
    net = -RESERVED - PROXY - COMPUTE + sum(released_calls)   # dispatch reserve + live + release
    assert net == -actual_spend, (
        f"net balance change {net} must equal -actual_spend {-actual_spend} "
        "(reserve fully released; no double-charge)")
