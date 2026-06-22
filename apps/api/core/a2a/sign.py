"""core/a2a/sign.py — signed Agent Card (JWS / ES256 over JCS).

Produces and verifies the A2A `signatures[]` block on an Agent Card. The signature
covers the card MINUS its own signatures field, canonicalized with JCS (RFC 8785),
as a detached JWS (the payload is the card itself, reconstructed by the verifier):

    signing_input = b64url(protected_header) || "." || b64url(JCS(card_sans_sig))
    signature     = ES256(signing_input)            # raw R||S, 64 bytes (JWS form)
    signatures[]  = [{ "protected": b64url(header), "signature": b64url(sig) }]

The protected header carries alg=ES256, the signing key's kid, and jku=APEX_JKU
(the brand-apex JWKS the verifier fetches — see keys.py for why issuer identity
sits at the apex while the card is served from the gateway).

This module decides neither card structure (card.py) nor message vocabulary
(serializer.py); it only signs/verifies bytes. Leak guard scans it like any other.
"""
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

from core.a2a.card import build_agent_card
from core.a2a.keys import APEX_JKU, SIGNING_ALG, get_active_signing_key

_COORD_BYTES = 32


# ── base64url + JCS canonicalization ──────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonicalize(card_without_sig: dict) -> bytes:
    """JCS (RFC 8785) canonical bytes of the card. Prefers a real RFC 8785
    implementation; falls back to sorted-compact JSON, which is byte-identical to
    JCS for our card's value types (strings, bools, arrays, objects, small ints —
    no floats, so ECMAScript number formatting never diverges)."""
    try:
        import rfc8785  # type: ignore
        return rfc8785.dumps(card_without_sig)
    except Exception:
        return json.dumps(
            card_without_sig, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


# ── ES256 (JWS raw R||S form, not DER) ────────────────────────────────────────

def _es256_sign(private_key: ec.EllipticCurvePrivateKey, signing_input: bytes) -> bytes:
    der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    return r.to_bytes(_COORD_BYTES, "big") + s.to_bytes(_COORD_BYTES, "big")


def _es256_verify(public_key: ec.EllipticCurvePublicKey, signing_input: bytes, sig: bytes) -> bool:
    if len(sig) != 2 * _COORD_BYTES:
        return False
    r = int.from_bytes(sig[:_COORD_BYTES], "big")
    s = int.from_bytes(sig[_COORD_BYTES:], "big")
    der = asym_utils.encode_dss_signature(r, s)
    try:
        public_key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
    y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def _signing_input(card: dict, protected_b64: str) -> bytes:
    payload = dict(card)
    payload.pop("signatures", None)          # signature never covers itself
    payload_b64 = _b64url(canonicalize(payload))
    return f"{protected_b64}.{payload_b64}".encode("ascii")


# ── sign / verify ─────────────────────────────────────────────────────────────

def sign_card(card: dict, kid: str, private_key: ec.EllipticCurvePrivateKey) -> dict:
    """Return a copy of `card` with a single ES256 signature attached. Pure: takes
    the already-loaded key, no DB. Replaces any existing signatures."""
    protected = {"alg": SIGNING_ALG, "kid": kid, "jku": APEX_JKU}
    protected_b64 = _b64url(
        json.dumps(protected, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _es256_sign(private_key, _signing_input(card, protected_b64))
    signed = dict(card)
    signed["signatures"] = [{"protected": protected_b64, "signature": _b64url(sig)}]
    return signed


def verify_card(card: dict, jwks: dict) -> bool:
    """Verify the card's signature against a JWKS (keys-by-kid). True only if a
    signature's kid resolves to a JWKS key AND ES256 verifies over the
    reconstructed signing input. No signatures, unknown kid, or any failure → False."""
    signatures = card.get("signatures") or []
    by_kid = {k.get("kid"): k for k in jwks.get("keys", []) if k.get("kid")}
    for sig_obj in signatures:
        protected_b64 = sig_obj.get("protected")
        sig_b64 = sig_obj.get("signature")
        if not protected_b64 or not sig_b64:
            continue
        try:
            header = json.loads(_b64url_decode(protected_b64))
        except Exception:
            continue
        jwk = by_kid.get(header.get("kid"))
        if not jwk:
            continue
        public_key = _public_key_from_jwk(jwk)
        signing_input = f"{protected_b64}.{_b64url(canonicalize(_card_sans_sig(card)))}".encode("ascii")
        if _es256_verify(public_key, signing_input, _b64url_decode(sig_b64)):
            return True
    return False


def _card_sans_sig(card: dict) -> dict:
    payload = dict(card)
    payload.pop("signatures", None)
    return payload


async def build_signed_card(db, **card_kwargs) -> dict:
    """Build Wayforth's Agent Card and sign it with the active key. The one call a
    well-known card handler makes."""
    card = build_agent_card(**card_kwargs)
    kid, private_key = await get_active_signing_key(db)
    return sign_card(card, kid, private_key)
