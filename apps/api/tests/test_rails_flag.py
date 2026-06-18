"""test_rails_flag.py — core.rails is the single source of truth for rail status.

Pins the launch model: rails are dark by default; the master flag lights
card/usdc; a per-rail flag overrides the master; x402 needs the flag AND a
funded settlement path; the legacy WAYFORTH_X402_ENABLED env is still honored.
"""
from __future__ import annotations

import pytest

from core import rails

_RAIL_ENVS = (
    "WAYFORTH_RAILS_LIVE", "WAYFORTH_RAIL_CARD", "WAYFORTH_RAIL_USDC",
    "WAYFORTH_RAIL_X402", "WAYFORTH_X402_ENABLED",
    "WAYFORTH_BASE_WALLET", "CDP_API_KEY_NAME", "CDP_API_KEY_PRIVATE_KEY",
    "X402_RELAYER_PRIVATE_KEY",
)


@pytest.fixture(autouse=True)
def _clean_rail_env(monkeypatch):
    for v in _RAIL_ENVS:
        monkeypatch.delenv(v, raising=False)
    yield


def test_all_dark_by_default():
    assert rails.rail_live("card") is False
    assert rails.rail_live("usdc") is False
    assert rails.rail_live("x402") is False
    assert rails.live_rails() == []


def test_master_flag_lights_card_and_usdc(monkeypatch):
    monkeypatch.setenv("WAYFORTH_RAILS_LIVE", "true")
    assert rails.rail_live("card") is True
    assert rails.rail_live("usdc") is True
    # x402 still dark — settlement not ready even with the master flag on.
    assert rails.rail_live("x402") is False
    assert rails.live_rails() == ["card", "usdc"]


def test_per_rail_override_beats_master(monkeypatch):
    monkeypatch.setenv("WAYFORTH_RAILS_LIVE", "true")
    monkeypatch.setenv("WAYFORTH_RAIL_CARD", "false")
    assert rails.rail_live("card") is False
    assert rails.rail_live("usdc") is True


def test_x402_needs_flag_and_settlement(monkeypatch):
    monkeypatch.setenv("WAYFORTH_RAIL_X402", "true")
    # Flag on but no funded path → still dark.
    assert rails.rail_live("x402") is False
    # Add a funded relayer path → now live.
    monkeypatch.setenv("WAYFORTH_BASE_WALLET", "0x00000000000000000000000000000000000000aa")
    monkeypatch.setenv("X402_RELAYER_PRIVATE_KEY", "0x" + "11" * 32)
    assert rails.rail_live("x402") is True


def test_legacy_x402_env_honored(monkeypatch):
    monkeypatch.setenv("WAYFORTH_X402_ENABLED", "true")
    monkeypatch.setenv("WAYFORTH_BASE_WALLET", "0x00000000000000000000000000000000000000aa")
    monkeypatch.setenv("X402_RELAYER_PRIVATE_KEY", "0x" + "11" * 32)
    assert rails.rail_live("x402") is True


def test_unknown_rail_is_never_live():
    assert rails.rail_live("paypal") is False


def test_status_payload_shape(monkeypatch):
    monkeypatch.setenv("WAYFORTH_RAILS_LIVE", "true")
    status = rails.rails_status()
    assert status["rails_live"] is True
    assert set(status["rails"].keys()) == {"card", "usdc", "x402"}
    assert "settlement_ready" in status["rails"]["x402"]
    assert status["rails"]["card"]["live"] is True
