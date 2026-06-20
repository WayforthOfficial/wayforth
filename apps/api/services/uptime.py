"""services/uptime.py — real uptime + incidents from UptimeRobot.

The status page's uptime number used to be a hardcoded 99.97 with an empty
incidents list (fabricated). The real source is UptimeRobot: this module calls
its getMonitors API with a READ-ONLY key, reads the Wayforth Gateway monitor's
30-day `custom_uptime_ratio`, and derives incidents from the monitor's event log
(real down events — e.g. a rank-service outage shows up, instead of a false
"none in 90 days").

Result is cached in-process for ~5 minutes so the public /system/status endpoint
never hammers UptimeRobot. Every failure mode falls back to "unmeasured"
(uptime=None, incidents=None) — we NEVER fabricate a number.

Config (backend env):
  UPTIMEROBOT_API_KEY       — read-only API key (also accepts UPTIMEROBOT_READ_ONLY_KEY)
  UPTIMEROBOT_MONITOR_ID    — optional: pin the exact monitor; otherwise the
                              monitor whose name contains "gateway"/"wayforth" is
                              used, else the first monitor returned.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("wayforth")

_API_URL = "https://api.uptimerobot.com/v2/getMonitors"
_CACHE_TTL = 300  # seconds (~5 min)
_HTTP_TIMEOUT = 6.0

# In-process cache: {"at": epoch, "value": snapshot}
_cache: dict = {}


def _api_key() -> str:
    return (
        os.environ.get("UPTIMEROBOT_API_KEY")
        or os.environ.get("UPTIMEROBOT_READ_ONLY_KEY")
        or ""
    )


def _unmeasured() -> dict:
    return {"uptime_30d": None, "uptime_source": "unmeasured", "incidents": None}


def _pick_monitor(monitors: list[dict]) -> dict | None:
    if not monitors:
        return None
    pinned = os.environ.get("UPTIMEROBOT_MONITOR_ID", "").strip()
    if pinned:
        for m in monitors:
            if str(m.get("id")) == pinned:
                return m
    for m in monitors:  # prefer the gateway/wayforth monitor by name
        name = (m.get("friendly_name") or "").lower()
        if "gateway" in name or "wayforth" in name:
            return m
    return monitors[0]


def _incidents_from_logs(monitor: dict, *, limit: int = 10) -> list[dict]:
    """Map UptimeRobot down-events (log type 1) to incident records."""
    incidents = []
    for log in monitor.get("logs", []) or []:
        if log.get("type") != 1:  # 1 = down (2 = up, 99 = paused)
            continue
        ts = log.get("datetime")
        started = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            if ts else None
        )
        reason = log.get("reason") or {}
        incidents.append({
            "started_at": started,
            "duration_seconds": int(log.get("duration") or 0),
            "reason": reason.get("detail") or reason.get("code") or "down",
        })
    incidents.sort(key=lambda i: i.get("started_at") or "", reverse=True)
    return incidents[:limit]


async def get_uptime_snapshot() -> dict:
    """Return {uptime_30d, uptime_source, incidents}. Cached ~5 min, fail-safe.

    Falls back to unmeasured (None) on any error or when no key is configured —
    never fabricates a value.
    """
    key = _api_key()
    if not key:
        return _unmeasured()

    now = time.time()
    cached = _cache.get("value")
    if cached is not None and (now - _cache.get("at", 0)) < _CACHE_TTL:
        return cached

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                _API_URL,
                data={
                    "api_key": key,
                    "format": "json",
                    "custom_uptime_ratios": "30",  # 30-day ratio
                    "logs": "1",
                },
                headers={"Cache-Control": "no-cache"},
            )
        data = resp.json()
        if data.get("stat") != "ok":
            logger.warning("uptimerobot getMonitors not ok: %s", data.get("error"))
            return _cache_and_return(_unmeasured(), now)

        monitor = _pick_monitor(data.get("monitors", []))
        if not monitor:
            return _cache_and_return(_unmeasured(), now)

        ratio_raw = monitor.get("custom_uptime_ratio")
        # custom_uptime_ratio is a string like "99.950" for the single window.
        uptime = None
        if ratio_raw not in (None, ""):
            try:
                uptime = round(float(str(ratio_raw).split("-")[0]), 3)
            except (ValueError, TypeError):
                uptime = None

        snapshot = {
            "uptime_30d": uptime,
            "uptime_source": "uptimerobot" if uptime is not None else "unmeasured",
            "incidents": _incidents_from_logs(monitor),
        }
        return _cache_and_return(snapshot, now)
    except Exception as exc:
        logger.warning("uptimerobot fetch failed: %s", exc)
        # Serve a stale cached value if we have one; else unmeasured.
        return cached if cached is not None else _unmeasured()


def _cache_and_return(snapshot: dict, now: float) -> dict:
    _cache["value"] = snapshot
    _cache["at"] = now
    return snapshot
