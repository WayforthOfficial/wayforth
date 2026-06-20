"""test_uptime_source.py — UptimeRobot uptime/incidents client (offline).

Locks in the honesty contract: no key or any failure → unmeasured (None), NEVER a
fabricated number; a real getMonitors payload → real ratio + real down-events.
"""
from __future__ import annotations

import asyncio

import pytest

from services import uptime


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for v in ("UPTIMEROBOT_API_KEY", "UPTIMEROBOT_READ_ONLY_KEY", "UPTIMEROBOT_MONITOR_ID"):
        monkeypatch.delenv(v, raising=False)
    uptime._cache.clear()
    yield
    uptime._cache.clear()


def test_no_key_is_unmeasured():
    snap = asyncio.run(uptime.get_uptime_snapshot())
    assert snap == {"uptime_30d": None, "uptime_source": "unmeasured", "incidents": None}


def test_pick_monitor_prefers_gateway():
    mons = [{"id": 1, "friendly_name": "Docs site"},
            {"id": 2, "friendly_name": "Wayforth Gateway"}]
    assert uptime._pick_monitor(mons)["id"] == 2


def test_pick_monitor_honors_pinned_id(monkeypatch):
    monkeypatch.setenv("UPTIMEROBOT_MONITOR_ID", "1")
    mons = [{"id": 1, "friendly_name": "Docs site"},
            {"id": 2, "friendly_name": "Wayforth Gateway"}]
    assert uptime._pick_monitor(mons)["id"] == 1


def test_incidents_only_down_events():
    mon = {"logs": [
        {"type": 1, "datetime": 1718000000, "duration": 540,
         "reason": {"code": "503", "detail": "rank-service down"}},
        {"type": 2, "datetime": 1718000540, "duration": 0, "reason": {"code": "200"}},
        {"type": 99, "datetime": 1717000000, "duration": 0, "reason": {}},
    ]}
    inc = uptime._incidents_from_logs(mon)
    assert len(inc) == 1
    assert inc[0]["reason"] == "rank-service down"
    assert inc[0]["duration_seconds"] == 540
    assert inc[0]["started_at"].endswith("+00:00")


def test_live_payload_parses(monkeypatch):
    """A mocked getMonitors response yields a real ratio + incidents (no network)."""
    monkeypatch.setenv("UPTIMEROBOT_API_KEY", "ro-test-key")

    class _Resp:
        def json(self):
            return {"stat": "ok", "monitors": [{
                "id": 777, "friendly_name": "Wayforth Gateway", "status": 2,
                "custom_uptime_ratio": "99.231",
                "logs": [{"type": 1, "datetime": 1718000000, "duration": 60,
                          "reason": {"detail": "timeout"}}],
            }]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(uptime.httpx, "AsyncClient", _Client)
    snap = asyncio.run(uptime.get_uptime_snapshot())
    assert snap["uptime_30d"] == 99.231
    assert snap["uptime_source"] == "uptimerobot"
    assert len(snap["incidents"]) == 1 and snap["incidents"][0]["reason"] == "timeout"


def test_api_error_falls_back_unmeasured(monkeypatch):
    monkeypatch.setenv("UPTIMEROBOT_API_KEY", "ro-test-key")

    class _Resp:
        def json(self): return {"stat": "fail", "error": {"message": "bad key"}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(uptime.httpx, "AsyncClient", _Client)
    snap = asyncio.run(uptime.get_uptime_snapshot())
    assert snap["uptime_30d"] is None
    assert snap["uptime_source"] == "unmeasured"
