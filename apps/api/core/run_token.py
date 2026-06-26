"""core/run_token.py — ephemeral per-run agent tokens (the ``wf_run_`` credential).

A run token is a short-lived, run-scoped credential injected into a hosted-agent
sandbox in place of the user's raw ``wf_live_`` key. It authorizes ONLY the agent's
legitimate gateway surface (/proxy, /x402/execute, /payments/rails) and is bound to
(user_id, agent_id, run_id), so a leaked token can't be replayed for a different
user/agent/run, can't outlive its run, and can't reach admin/billing/auth.

Format: ``wf_run_<JWT>`` — HS256, signed with ``RUN_TOKEN_SIGNING_SECRET`` (the
gateway is the sole issuer AND verifier, so a symmetric secret is sufficient; no
key distribution). Verification here is stateless (signature + ``typ`` + ``exp``).
Run-binding and revocation are enforced at /proxy against the live ``agent_runs``
row (Step 2) — NOT in this module.

This module is pure crypto/claims: no DB, no request, no charge-path. It is minted
at dispatch (Step 3) and verified at /proxy (Step 2). Importing it changes nothing.

Secret handling / rotation:
  • mint always signs with ``RUN_TOKEN_SIGNING_SECRET`` (current).
  • verify accepts ``RUN_TOKEN_SIGNING_SECRET`` then ``RUN_TOKEN_SIGNING_SECRET_PREV``
    (an optional overlap window so a rotation doesn't invalidate in-flight runs).
  • the secret is a strong random value set in the Railway env, never committed.
"""
from __future__ import annotations

import os
import secrets
import time

import jwt

ALG = "HS256"
TOKEN_TYP = "wf_run"
RUN_TOKEN_PREFIX = "wf_run_"

# Default lifetime ceiling: the cloud-run hard timeout (1800s) + a 60s skew /
# settlement buffer. The *effective* expiry is shorter — the run-status check at
# /proxy revokes the token the moment the run leaves an active state.
RUN_TOKEN_TTL_SECONDS = 1860

# Scope vocabulary — the approved agent allowlist. Routes check membership; anything
# not granted is default-denied. Admin / billing / auth / key-management are never
# representable here.
SCOPE_PROXY = "proxy"
SCOPE_X402_EXECUTE = "x402_execute"
SCOPE_PAYMENTS_RAILS = "payments_rails"
DEFAULT_RUN_SCOPE: tuple[str, ...] = (SCOPE_PROXY, SCOPE_X402_EXECUTE, SCOPE_PAYMENTS_RAILS)

# Claims a valid run token must carry (binding + lifetime essentials).
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "agent_id", "run_id"]


class RunTokenError(Exception):
    """Raised when a run token cannot be minted (e.g. no signing secret)."""


def _current_secret() -> str:
    return os.environ.get("RUN_TOKEN_SIGNING_SECRET", "")


def _verify_secrets() -> list[str]:
    # Current first, then the previous secret (rotation overlap). Empty entries
    # are skipped by the verify loop.
    return [
        os.environ.get("RUN_TOKEN_SIGNING_SECRET", ""),
        os.environ.get("RUN_TOKEN_SIGNING_SECRET_PREV", ""),
    ]


def mint_run_token(
    user_id: str,
    agent_id: str,
    run_id: str,
    *,
    scope: tuple[str, ...] | list[str] | None = None,
    ttl_seconds: int = RUN_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint ``wf_run_<JWT>`` bound to (user_id, agent_id, run_id).

    `scope` defaults to DEFAULT_RUN_SCOPE. `ttl_seconds` sets exp = iat + ttl (the
    caller passes the run's timeout + buffer). `now` overrides the issue time (tests).
    Raises RunTokenError if no signing secret is configured.
    """
    secret = _current_secret()
    if not secret:
        raise RunTokenError("RUN_TOKEN_SIGNING_SECRET is not configured")

    iat = int(now if now is not None else time.time())
    payload = {
        "typ": TOKEN_TYP,
        "sub": str(user_id),
        "agent_id": str(agent_id),
        "run_id": str(run_id),
        "scope": list(scope if scope is not None else DEFAULT_RUN_SCOPE),
        "iat": iat,
        "exp": iat + int(ttl_seconds),
        "jti": secrets.token_urlsafe(9),
    }
    return RUN_TOKEN_PREFIX + jwt.encode(payload, secret, algorithm=ALG)


def verify_run_token(raw: str | None, *, leeway: int = 0) -> dict | None:
    """Return the claims dict iff `raw` is a structurally-valid, correctly-signed,
    unexpired ``wf_run_`` token; otherwise None.

    Validates ONLY the token itself (prefix, signature against current-or-previous
    secret, exp/iat, required claims, typ). Scope membership and run-binding are the
    caller's job (token_has_scope + the /proxy agent_runs cross-check). Restricted to
    HS256 to block algorithm-confusion. Never raises — a bad token is just None.
    """
    if not raw or not raw.startswith(RUN_TOKEN_PREFIX):
        return None
    token = raw[len(RUN_TOKEN_PREFIX):]
    for secret in _verify_secrets():
        if not secret:
            continue
        try:
            claims = jwt.decode(
                token, secret, algorithms=[ALG], leeway=leeway,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError:
            continue  # wrong secret / expired / malformed — try the next secret
        if claims.get("typ") != TOKEN_TYP:
            return None  # signed by us but not a run token — reject, don't fall through
        return claims
    return None


def token_has_scope(claims: dict, scope_name: str) -> bool:
    """True iff the (already-verified) claims grant `scope_name`. Default-deny."""
    return scope_name in (claims.get("scope") or [])
