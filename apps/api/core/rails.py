"""core/rails.py — ONE backend source of truth for payment-rail live status.

Every template, pay path, and rail advertisement reads rail-live status from
here — never from a scattered per-module env flag. There are three rails:

    card   — Stripe subscription / top-up billing
    usdc   — USDC subscription billing (Coinbase Commerce / on-chain transfer)
    x402   — x402 pay-per-call (EIP-3009 transferWithAuthorization on Base)

Launch model: rails ship built present-tense but DARK. A rail goes live by
flipping its launch flag exactly once at production launch — no code sweep, no
redeploy of call sites. Flip the master `WAYFORTH_RAILS_LIVE=true` to light all
three at once, or a per-rail flag (`WAYFORTH_RAIL_CARD` / `_USDC` / `_X402`) for
a staged rollout. A per-rail flag, when set, overrides the master for that rail.

x402 has an extra hard gate beyond its flag: even with the flag on, the rail is
only live when the EIP-3009/CDP settlement client is importable AND CDP signing
keys are configured (see `services.x402_client.x402_settlement_ready`). This
prevents the forged-envelope free-execution hole (FINDING-001) from ever being
reachable just because someone set an env var. The flag is necessary but not
sufficient for x402.
"""
from __future__ import annotations

import os

RAILS = ("card", "usdc", "x402")

# Legacy env honored for backward-compat so an already-deployed instance that
# set WAYFORTH_X402_ENABLED keeps working until it migrates to the rail flags.
_LEGACY_X402_ENV = "WAYFORTH_X402_ENABLED"


def _env_true(name: str) -> bool | None:
    """Return True/False for a set boolean env var, or None when unset.

    None (unset) is meaningful: it means "defer to the master flag".
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() == "true"


def _master_live() -> bool:
    return _env_true("WAYFORTH_RAILS_LIVE") is True


def _rail_flag(rail: str) -> bool:
    """Whether the rail's launch flag is on — per-rail override, else master."""
    per_rail = _env_true(f"WAYFORTH_RAIL_{rail.upper()}")
    if per_rail is not None:
        return per_rail
    # x402 also honors the pre-existing kill-switch env as an override.
    if rail == "x402":
        legacy = _env_true(_LEGACY_X402_ENV)
        if legacy is not None:
            return legacy
    return _master_live()


def _x402_settlement_ready() -> bool:
    """x402's extra hard gate: real on-chain settlement must be wired + funded."""
    try:
        from services.x402_client import x402_settlement_ready
    except Exception:
        return False
    try:
        return bool(x402_settlement_ready())
    except Exception:
        return False


def rail_live(rail: str) -> bool:
    """Single predicate: is `rail` live right now?

    card/usdc → just the launch flag. x402 → launch flag AND settlement-ready.
    """
    rail = rail.lower()
    if rail not in RAILS:
        return False
    if not _rail_flag(rail):
        return False
    if rail == "x402":
        return _x402_settlement_ready()
    return True


def live_rails() -> list[str]:
    """The rails that are live right now, in canonical order."""
    return [r for r in RAILS if rail_live(r)]


def rails_status() -> dict:
    """Full rail-status snapshot — the payload `GET /payments/rails` returns.

    `flag` is whether the launch flag is on; `live` is the effective status a
    caller should trust (x402.live can be False while x402.flag is True when
    settlement isn't ready yet).
    """
    status: dict = {
        "rails_live": _master_live(),
        "rails": {},
        "live": live_rails(),
    }
    for r in RAILS:
        entry = {"flag": _rail_flag(r), "live": rail_live(r)}
        if r == "x402":
            entry["settlement_ready"] = _x402_settlement_ready()
        status["rails"][r] = entry
    return status
