"""test_a2a_interop_signing.py — card-signature interop gate (PR A).

Proves our signed card is byte-compatible with the STOCK a2a-sdk, BOTH directions:
  • sign-ours → verify-SDK   (a real third-party verifier accepts our signature)
  • sign-SDK  → verify-ours  (our verifier accepts a real third-party signature)
plus canonical-bytes equality (the exclude_defaults/_clean_empty replication).

The bidirectional round-trip is the point: a one-way "it verifies" check misses a
single-field canonicalization divergence (e.g. the nested securitySchemes.*.type
default), which silently breaks third-party verification.

Skipped unless a2a-sdk is importable, so the normal suite needs no heavy dep. Run
the real gate with:
    uv run --with "a2a-sdk==0.3.26" --with pytest --with pytest-asyncio \
        pytest tests/test_a2a_interop_signing.py -q
"""
from __future__ import annotations

import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

# Gate dependency — skip cleanly when the reference SDK isn't installed.
a2a_types = pytest.importorskip("a2a.types")
a2a_signing = pytest.importorskip("a2a.utils.signing")
sdk_helpers = pytest.importorskip("a2a.utils.helpers")

from core.a2a.card import build_agent_card                 # noqa: E402
from core.a2a.keys import APEX_JKU, generate_signing_key    # noqa: E402
from core.a2a.sign import canonicalize_agent_card, sign_card, verify_card  # noqa: E402

AgentCard = a2a_types.AgentCard


def _card() -> dict:
    return build_agent_card(
        name="Wayforth Gateway", description="d",
        url="https://gateway.wayforth.io/a2a", version="0.9.2",
        skills=[{"id": "execute-api", "name": "Execute API",
                 "description": "run apis", "tags": ["api"]}],
    )


def test_canonical_bytes_match_sdk():
    card = _card()
    ours = canonicalize_agent_card(card)
    theirs = sdk_helpers.canonicalize_agent_card(AgentCard.model_validate(card))
    assert ours == theirs, f"\n OURS: {ours}\n SDK : {theirs}"


def test_sdk_verifies_our_signature():
    priv = ec.generate_private_key(ec.SECP256R1())
    kid, _, _ = generate_signing_key()
    signed = sign_card(_card(), kid, priv)

    verifier = a2a_signing.create_signature_verifier(
        key_provider=lambda kid_, jku_: priv.public_key(), algorithms=["ES256"])
    # Raises on failure; returns None on success.
    verifier(AgentCard.model_validate(signed))


def test_we_verify_sdk_signature():
    priv = ec.generate_private_key(ec.SECP256R1())
    _, _, jwk = generate_signing_key()
    # Re-key the JWK to this private key so our JWKS lookup matches.
    from core.a2a.keys import _public_jwk_coords, jwk_thumbprint
    coords = _public_jwk_coords(priv.public_key())
    kid = jwk_thumbprint(coords)
    jwk = {**coords, "kid": kid, "use": "sig", "alg": "ES256"}

    signer = a2a_signing.create_agent_card_signer(
        signing_key=priv,
        protected_header={"alg": "ES256", "kid": kid, "jku": APEX_JKU},
        header={})
    sdk_signed = signer(AgentCard.model_validate(_card()))
    served = sdk_signed.model_dump(by_alias=True, exclude_none=True)

    assert verify_card(served, {"keys": [jwk]}) is True
