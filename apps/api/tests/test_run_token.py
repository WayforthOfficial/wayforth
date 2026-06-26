"""test_run_token.py — Step 1 of the ephemeral per-run token build.

Full matrix for core.run_token: round-trip, expiry, tamper, wrong-secret,
typ/prefix, scope, and rotation overlap. Pure crypto/claims — no DB, no request.
"""
from __future__ import annotations

import time

import jwt
import pytest

from core import run_token as rt

USER = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"
SECRET = "test-run-secret-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET_PREV", raising=False)
    return SECRET


# ── round-trip ────────────────────────────────────────────────────────────────

def test_round_trip(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN)
    assert tok.startswith("wf_run_")
    claims = rt.verify_run_token(tok)
    assert claims is not None
    assert claims["typ"] == "wf_run"
    assert claims["sub"] == USER
    assert claims["agent_id"] == AGENT
    assert claims["run_id"] == RUN
    assert claims["scope"] == list(rt.DEFAULT_RUN_SCOPE)
    assert claims["exp"] > claims["iat"]
    assert claims.get("jti")


# ── expiry ────────────────────────────────────────────────────────────────────

def test_expired_token_rejected(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN, ttl_seconds=-1)  # exp before iat
    assert rt.verify_run_token(tok) is None


def test_not_yet_expired_still_valid_with_short_ttl(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN, ttl_seconds=5)
    assert rt.verify_run_token(tok) is not None


def test_expiry_uses_minted_now(secret):
    # Minted as-if 1h ago with a 60s ttl → long expired now.
    tok = rt.mint_run_token(USER, AGENT, RUN, now=time.time() - 3600, ttl_seconds=60)
    assert rt.verify_run_token(tok) is None


# ── tamper ────────────────────────────────────────────────────────────────────

def test_tampered_payload_rejected(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN)
    body = tok[len("wf_run_"):]
    header_b64, payload_b64, sig_b64 = body.split(".")
    # flip a character in the payload segment → signature no longer matches
    flipped = payload_b64[:-2] + ("AA" if payload_b64[-2:] != "AA" else "BB")
    tampered = "wf_run_" + ".".join([header_b64, flipped, sig_b64])
    assert rt.verify_run_token(tampered) is None


def test_tampered_signature_rejected(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN)
    assert rt.verify_run_token(tok[:-3] + "zzz") is None


# ── wrong secret ──────────────────────────────────────────────────────────────

def test_wrong_secret_rejected(secret, monkeypatch):
    tok = rt.mint_run_token(USER, AGENT, RUN)            # signed with SECRET
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", "a-totally-different-secret-xxxx")
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET_PREV", raising=False)
    assert rt.verify_run_token(tok) is None


def test_mint_requires_secret(monkeypatch):
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET", raising=False)
    with pytest.raises(rt.RunTokenError):
        rt.mint_run_token(USER, AGENT, RUN)


def test_verify_returns_none_without_secret(monkeypatch):
    # mint under a secret, then verify with no secret configured at all.
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)
    tok = rt.mint_run_token(USER, AGENT, RUN)
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET_PREV", raising=False)
    assert rt.verify_run_token(tok) is None


# ── typ / prefix ──────────────────────────────────────────────────────────────

def test_missing_prefix_rejected(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN)
    raw_jwt = tok[len("wf_run_"):]
    assert rt.verify_run_token(raw_jwt) is None          # JWT without the wf_run_ prefix
    assert rt.verify_run_token("wf_live_" + raw_jwt) is None  # a real-key prefix


def test_non_token_inputs_rejected(secret):
    for bad in (None, "", "wf_live_abc123", "wf_run_", "wf_run_not.a.jwt", "garbage"):
        assert rt.verify_run_token(bad) is None


def test_wrong_typ_rejected(secret):
    # A JWT signed by us but with the wrong typ must NOT pass (e.g. some other token
    # type that happens to share the secret).
    iat = int(time.time())
    payload = {"typ": "something_else", "sub": USER, "agent_id": AGENT, "run_id": RUN,
               "scope": ["proxy"], "iat": iat, "exp": iat + 60}
    forged = "wf_run_" + jwt.encode(payload, SECRET, algorithm="HS256")
    assert rt.verify_run_token(forged) is None


def test_missing_required_claims_rejected(secret):
    # Signed by us, correct typ, but missing run_id/agent_id binding → reject.
    iat = int(time.time())
    payload = {"typ": "wf_run", "sub": USER, "scope": ["proxy"], "iat": iat, "exp": iat + 60}
    forged = "wf_run_" + jwt.encode(payload, SECRET, algorithm="HS256")
    assert rt.verify_run_token(forged) is None


def test_alg_confusion_blocked(secret):
    # A token using a different alg must not verify (algorithms restricted to HS256).
    iat = int(time.time())
    payload = {"typ": "wf_run", "sub": USER, "agent_id": AGENT, "run_id": RUN,
               "scope": ["proxy"], "iat": iat, "exp": iat + 60}
    other = "wf_run_" + jwt.encode(payload, SECRET, algorithm="HS384")
    assert rt.verify_run_token(other) is None


# ── scope ─────────────────────────────────────────────────────────────────────

def test_default_scope_is_the_allowlist(secret):
    claims = rt.verify_run_token(rt.mint_run_token(USER, AGENT, RUN))
    assert rt.token_has_scope(claims, rt.SCOPE_PROXY)
    assert rt.token_has_scope(claims, rt.SCOPE_X402_EXECUTE)
    assert rt.token_has_scope(claims, rt.SCOPE_PAYMENTS_RAILS)
    # default-deny everything outside the allowlist
    for denied in ("admin", "billing", "run", "auth", "keys", "execute"):
        assert not rt.token_has_scope(claims, denied)


def test_custom_scope_grants_only_what_was_minted(secret):
    claims = rt.verify_run_token(
        rt.mint_run_token(USER, AGENT, RUN, scope=(rt.SCOPE_PROXY,)))
    assert rt.token_has_scope(claims, rt.SCOPE_PROXY)
    assert not rt.token_has_scope(claims, rt.SCOPE_X402_EXECUTE)
    assert not rt.token_has_scope(claims, rt.SCOPE_PAYMENTS_RAILS)


def test_token_has_scope_default_deny_on_empty(secret):
    assert rt.token_has_scope({}, rt.SCOPE_PROXY) is False
    assert rt.token_has_scope({"scope": None}, rt.SCOPE_PROXY) is False


# ── rotation overlap ──────────────────────────────────────────────────────────

def test_rotation_overlap_previous_secret_still_verifies(monkeypatch):
    # Mint under the OLD secret…
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", "old-secret-oooooooooooooooooooooo")
    tok = rt.mint_run_token(USER, AGENT, RUN)
    # …rotate: current becomes NEW, old moves to _PREV (overlap window).
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", "new-secret-nnnnnnnnnnnnnnnnnnnnnn")
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET_PREV", "old-secret-oooooooooooooooooooooo")
    claims = rt.verify_run_token(tok)
    assert claims is not None and claims["sub"] == USER  # accepted via the previous secret


def test_rotation_new_secret_mints_and_verifies(monkeypatch):
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", "new-secret-nnnnnnnnnnnnnnnnnnnnnn")
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET_PREV", "old-secret-oooooooooooooooooooooo")
    assert rt.verify_run_token(rt.mint_run_token(USER, AGENT, RUN)) is not None


def test_unknown_secret_not_in_current_or_prev_rejected(monkeypatch):
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", "secretA-aaaaaaaaaaaaaaaaaaaaaaaaaa")
    tok = rt.mint_run_token(USER, AGENT, RUN)
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", "secretB-bbbbbbbbbbbbbbbbbbbbbbbbbb")
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET_PREV", "secretC-cccccccccccccccccccccccc")
    assert rt.verify_run_token(tok) is None  # signed by A, but only B/C are accepted
