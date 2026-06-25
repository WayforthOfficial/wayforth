"""core/a2a/sign.py — signed Agent Card, byte-compatible with the a2a-sdk verifier.

Produces and verifies the A2A `signatures[]` block. The scheme is deliberately
IDENTICAL to a2a-sdk 0.3.x (a2a.utils.signing) so that our signed card verifies
under ANY stock a2a-sdk verifier — a signature only our own verifier accepted
would be theatre. Verified byte-exact, both directions, in the interop gate
(sign-ours → verify-SDK and sign-SDK → verify-ours).

The signature is a detached JWS (PyJWT) over the canonicalized card:
    canonical   = canonicalize_agent_card(card)         # == the SDK's function
    jws         = jwt.encode(json.loads(canonical), key, ES256, headers={kid,jku})
    signatures  = [{ protected, signature }]            # payload dropped (detached)
The verifier recomputes `canonical` from the received card, rebuilds
`protected.b64url(canonical).signature`, and PyJWT-verifies — exactly as
a2a.utils.signing.create_signature_verifier does.

canonicalize_agent_card mirrors the SDK's:
    model_dump(exclude={'signatures'}, exclude_defaults=True, exclude_none=True,
               by_alias=True)  →  _clean_empty  →  json.dumps(sort_keys, compact)
Replicated without importing the SDK at runtime (no SDK in prod deps). The
exclude_defaults half is card-structure knowledge and lives in
card.strip_sdk_signing_defaults(); this module supplies only the structure-
agnostic empty-cleaning + canonical JSON + JWS. The bidirectional interop check
(sign-ours ↔ verify-SDK) proves the result byte-exact.

NOTE on the AP2 slot: an EMPTY extensions placeholder is removed by _clean_empty
before signing — the empty slot carries NO signed meaning. When AP2 populates it
with a real AgentExtension it stops being empty, _clean_empty keeps it, and the
signed bytes legitimately change. That is expected, not a regression: AP2's PR
re-signs and any cached signature over the empty-slot card is correctly
superseded. The signed card commits only to non-empty content. (See card.py.)

This module decides neither card structure (card.py) nor message vocabulary
(serializer.py); leak guard scans it like any other a2a module.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from core.a2a.card import build_agent_card, strip_sdk_signing_defaults
from core.a2a.keys import SIGNING_ALG, SIGNING_JKU, get_active_signing_key


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ── canonicalization: byte-identical to a2a.utils.helpers.canonicalize_agent_card ─

def _clean_empty(d: Any) -> Any:
    """Recursively remove empty strings, lists, and dicts (and None). Mirrors the
    SDK helper exactly, including the `cleaned or None` empty-collapse."""
    if isinstance(d, dict):
        cleaned = {k: cv for k, v in d.items() if (cv := _clean_empty(v)) is not None}
        return cleaned or None
    if isinstance(d, list):
        cleaned = [cv for v in d if (cv := _clean_empty(v)) is not None]
        return cleaned or None
    if isinstance(d, str) and not d:
        return None
    return d


def canonicalize_agent_card(card: dict) -> str:
    """RFC 8785-style canonical JSON of the card MINUS its signatures, replicating
    the SDK pipeline: card.strip_sdk_signing_defaults (exclude_defaults) →
    _clean_empty (exclude_none + empty-collapse) → sorted compact json. The
    signing/verification payload."""
    cleaned = _clean_empty(strip_sdk_signing_defaults(card)) or {}
    return json.dumps(cleaned, separators=(",", ":"), sort_keys=True)


# ── sign / verify (PyJWT, matching the SDK signer/verifier) ───────────────────

def sign_card(card: dict, kid: str, private_key: ec.EllipticCurvePrivateKey) -> dict:
    """Return a copy of `card` with one ES256 signature attached. Built with PyJWT
    exactly as a2a.utils.signing.create_agent_card_signer does, so a stock a2a-sdk
    verifier accepts it. Replaces any existing signatures."""
    canonical = canonicalize_agent_card(card)
    payload = json.loads(canonical)
    jws = jwt.encode(
        payload=payload, key=private_key, algorithm=SIGNING_ALG,
        headers={"kid": kid, "jku": SIGNING_JKU},
    )
    protected, _payload_b64, signature = jws.split(".")
    signed = dict(card)
    signed["signatures"] = [{"protected": protected, "signature": signature}]
    return signed


def verify_card(card: dict, jwks: dict) -> bool:
    """True iff a signature's kid resolves to a JWKS key AND ES256 verifies over
    the reconstructed `protected.b64url(canonical).signature` token — the exact
    reconstruction a2a-sdk's verifier performs. Unsigned, unknown kid, tampered,
    or wrong-key → False (restricted to ES256 to block alg-confusion)."""
    signatures = card.get("signatures") or []
    by_kid = {k.get("kid"): k for k in jwks.get("keys", []) if k.get("kid")}
    encoded_payload = _b64url(canonicalize_agent_card(card).encode("utf-8"))
    for sig_obj in signatures:
        protected_b64 = sig_obj.get("protected")
        signature_b64 = sig_obj.get("signature")
        if not protected_b64 or not signature_b64:
            continue
        try:
            header = json.loads(_b64url_decode(protected_b64))
        except Exception:
            continue
        jwk = by_kid.get(header.get("kid"))
        if not jwk:
            continue
        try:
            key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
            token = f"{protected_b64}.{encoded_payload}.{signature_b64}"
            jwt.decode(token, key=key, algorithms=[SIGNING_ALG],
                       options={"verify_aud": False, "verify_exp": False})
            return True
        except Exception:
            continue
    return False


async def build_signed_card(db, **card_kwargs) -> dict:
    """Build Wayforth's Agent Card and sign it with the active key — the one call a
    well-known card handler makes."""
    card = build_agent_card(**card_kwargs)
    kid, private_key = await get_active_signing_key(db)
    return sign_card(card, kid, private_key)
