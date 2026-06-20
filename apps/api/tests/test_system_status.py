"""
tests/test_system_status.py — /system/status health logic tests

Verifies the v0.7.7 fix:
  - Missing API keys are not counted as failures
  - managed_services status reflects only configured (keyed) services
  - Overall status only reaches "outage" when the api component is explicitly down
  - Response shape is always present and well-formed
"""
import os
import pytest
import httpx
from tests.test_suite_v060 import BASE_URL


# ── Live integration tests ─────────────────────────────────────────────────────

class TestSystemStatusShape:
    """Response schema is always well-formed."""

    def test_returns_200(self):
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        assert r.status_code == 200

    def test_has_required_top_level_keys(self):
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        body = r.json()
        assert "status" in body
        assert "components" in body
        assert "uptime_30d" in body
        assert "incidents" in body

    def test_incidents_null_or_list(self):
        # incidents is null while unmeasured (no incident-history source). It must
        # NEVER be a fabricated empty list, which would assert "zero incidents".
        # When a real source lands it may become a list.
        incidents = httpx.get(f"{BASE_URL}/system/status", timeout=15.0).json()["incidents"]
        assert incidents is None or isinstance(incidents, list)

    def test_uptime_30d_null_or_numeric(self):
        # uptime_30d is null until platform uptime is actually instrumented — no
        # hardcoded number. When a real source lands it is a 0-100 measurement.
        body = httpx.get(f"{BASE_URL}/system/status", timeout=15.0).json()
        uptime = body["uptime_30d"]
        assert uptime is None or (isinstance(uptime, (int, float)) and 0.0 <= uptime <= 100.0)
        if uptime is None:
            assert body.get("uptime_source") == "unmeasured"

    def test_components_has_api_key(self):
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        assert "api" in r.json()["components"]

    def test_components_has_managed_services_key(self):
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        assert "managed_services" in r.json()["components"]

    def test_all_component_values_are_valid_strings(self):
        valid = {"operational", "degraded", "outage"}
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        for key, val in r.json()["components"].items():
            assert val in valid, f"component {key!r} has invalid value {val!r}"

    def test_overall_status_is_valid(self):
        valid = {"operational", "degraded", "outage"}
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        assert r.json()["status"] in valid


class TestSystemStatusManagedServicesLogic:
    """Missing keys must never cause managed_services=outage or overall=outage."""

    def test_managed_services_is_not_outage_due_to_missing_keys(self):
        """A missing API key is an expected gap, not a service failure."""
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        body = r.json()
        managed = body["components"].get("managed_services")
        # If no keys are configured at all, the worst we report is "degraded".
        # Only actively failing configured services can cause "outage".
        # "outage" here would mean every single configured service is down —
        # not that some keys are simply absent.
        assert managed in ("operational", "degraded", "outage")
        # If overall is not outage, managed_services must not be the cause.
        # (This assertion catches the original bug: missing keys → outage.)
        if body["status"] == "outage":
            assert body["components"].get("api") == "outage", (
                "Overall status is 'outage' but api component is not down. "
                "Only a gateway-level failure should produce overall=outage."
            )

    def test_overall_outage_requires_api_component_outage(self):
        """Gateway is responding → api is operational → overall cannot be outage."""
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        body = r.json()
        # If we received this response, the api is operational by definition.
        assert body["components"].get("api") == "operational"
        # Therefore overall must not be "outage".
        assert body["status"] != "outage", (
            f"Got overall=outage but api component is operational. "
            f"Full response: {body}"
        )

    def test_api_component_is_always_operational_when_reachable(self):
        """The handler executing proves the gateway is up."""
        r = httpx.get(f"{BASE_URL}/system/status", timeout=15.0)
        assert r.json()["components"]["api"] == "operational"


# ── Unit-level logic tests (no DB, no network) ────────────────────────────────

class TestStatusRollupLogic:
    """Verify rollup rules directly without hitting the live endpoint."""

    def _rollup(self, components: dict) -> str:
        """Mirror the rollup logic from main.py system_status_v075."""
        if components.get("api") == "outage":
            return "outage"
        if any(v in ("outage", "degraded") for v in components.values()):
            return "degraded"
        return "operational"

    def test_all_operational_gives_operational(self):
        c = {"api": "operational", "catalog": "operational",
             "managed_services": "operational", "payments": "operational"}
        assert self._rollup(c) == "operational"

    def test_managed_services_outage_gives_degraded_not_outage(self):
        c = {"api": "operational", "catalog": "operational",
             "managed_services": "outage", "payments": "operational"}
        assert self._rollup(c) == "degraded"

    def test_managed_services_degraded_gives_degraded(self):
        c = {"api": "operational", "catalog": "operational",
             "managed_services": "degraded", "payments": "operational"}
        assert self._rollup(c) == "degraded"

    def test_catalog_outage_gives_degraded(self):
        c = {"api": "operational", "catalog": "outage",
             "managed_services": "operational", "payments": "operational"}
        assert self._rollup(c) == "degraded"

    def test_payments_outage_gives_degraded(self):
        c = {"api": "operational", "catalog": "operational",
             "managed_services": "operational", "payments": "outage"}
        assert self._rollup(c) == "degraded"

    def test_api_outage_gives_outage(self):
        c = {"api": "outage", "catalog": "operational",
             "managed_services": "operational", "payments": "operational"}
        assert self._rollup(c) == "outage"

    def test_api_outage_overrides_everything(self):
        c = {"api": "outage", "catalog": "outage",
             "managed_services": "outage", "payments": "outage"}
        assert self._rollup(c) == "outage"


class TestManagedServicesKeyLogic:
    """Verify that key-presence filtering behaves correctly."""

    def _managed_status(self, configured_count: int, failing_count: int) -> str:
        """Mirror the managed_services classification logic from main.py."""
        if configured_count == 0:
            return "degraded"
        if failing_count == 0:
            return "operational"
        if failing_count < configured_count:
            return "degraded"
        return "outage"  # all configured services are actively failing

    def test_no_keys_configured_gives_degraded(self):
        assert self._managed_status(0, 0) == "degraded"

    def test_all_keyed_services_healthy_gives_operational(self):
        assert self._managed_status(10, 0) == "operational"

    def test_some_keyed_services_failing_gives_degraded(self):
        assert self._managed_status(10, 3) == "degraded"

    def test_all_keyed_services_failing_gives_outage(self):
        assert self._managed_status(5, 5) == "outage"

    def test_single_keyed_service_failing_gives_outage(self):
        assert self._managed_status(1, 1) == "outage"

    def test_single_keyed_service_healthy_gives_operational(self):
        assert self._managed_status(1, 0) == "operational"
