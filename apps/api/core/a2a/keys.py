"""core/a2a/keys.py — Option-C A2A signing-key management.

The gateway holds ONE active EC P-256 (ES256) keypair for signing its Agent Card,
plus >=1 retiring keypair during a rotation overlap. Design decisions (approved):

  • OPTION C — the private key lives in Postgres, Fernet-encrypted with the SAME
    versioned layer as api_keys (core.auth.encrypt_api_key / decrypt_api_key).
    Plaintext private bytes never touch the DB and never leave the gateway. No new
    secret store; key_version lets us re-encrypt under a rotated ENCRYPTION_KEY.

  • SINGLE SOURCE OF TRUTH — the gateway generates and serves the JWKS. The brand
    apex (APEX_JKU) is the issuer identity in the JWS header `jku`; the apex
    transparently REWRITES to the gateway JWKS, so a key rotation requires no apex
    or Lovable deploy — only a DB write here.

  • >=2-KEY ROTATION WINDOW — JWKS publishes active + retiring public keys, so a
    verifier that fetched the old key just before a rotation still validates the
    last card it cached. rotate_signing_key() enforces the overlap.

This module owns key lifecycle + JWKS. It produces NO card structure and NO
message wire vocabulary, so it stays clear of the serializer/card authorities;
the leak guard scans it for both and must stay green.
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core.auth import decrypt_api_key, encrypt_api_key

# Issuer identity placed in the card signature's JWS `jku` header. The card is
# SERVED from the gateway, but the issuer is the brand apex; the apex rewrites to
# the gateway JWKS. Issuer identity and serving endpoint are deliberately separate.
APEX_JKU = "https://wayforth.io/.well-known/jwks.json"

SIGNING_ALG = "ES256"
SIGNING_CRV = "P-256"
_COORD_BYTES = 32  # P-256 field element width


# ── JWK / thumbprint primitives (pure) ────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _public_jwk_coords(public_key: ec.EllipticCurvePublicKey) -> dict:
    nums = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": SIGNING_CRV,
        "x": _b64url(nums.x.to_bytes(_COORD_BYTES, "big")),
        "y": _b64url(nums.y.to_bytes(_COORD_BYTES, "big")),
    }


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint: base64url(SHA-256(canonical JWK)). The canonical form
    is the required members only, lexicographically ordered, no whitespace."""
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"), sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def generate_signing_key() -> tuple[str, ec.EllipticCurvePrivateKey, dict]:
    """New EC P-256 keypair. Returns (kid, private_key, public_jwk). kid is the
    key's own RFC 7638 thumbprint — content-addressed, so it is stable and
    independently verifiable from the public JWK alone."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    coords = _public_jwk_coords(private_key.public_key())
    kid = jwk_thumbprint(coords)
    public_jwk = {**coords, "kid": kid, "use": "sig", "alg": SIGNING_ALG}
    return kid, private_key, public_jwk


def _encrypt_private(private_key: ec.EllipticCurvePrivateKey) -> tuple[str, int]:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return encrypt_api_key(pem)  # -> (ciphertext, key_version)


def _decrypt_private(ciphertext: str, key_version: int) -> ec.EllipticCurvePrivateKey:
    pem = decrypt_api_key(ciphertext, key_version)
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _as_dict(jwk) -> dict:
    return jwk if isinstance(jwk, dict) else json.loads(jwk)


# ── DB-backed lifecycle ───────────────────────────────────────────────────────

async def provision_signing_key(db) -> dict:
    """Ensure exactly one active signing key exists; return its public JWK.
    Idempotent: a no-op when one is already active (safe to call every startup).
    Tolerates a concurrent provision via the one-active partial unique index."""
    row = await db.fetchrow(
        "SELECT public_jwk FROM a2a_signing_keys WHERE status = 'active' LIMIT 1")
    if row:
        return _as_dict(row["public_jwk"])

    kid, private_key, public_jwk = generate_signing_key()
    ciphertext, version = _encrypt_private(private_key)
    try:
        await db.execute(
            "INSERT INTO a2a_signing_keys "
            "(kid, alg, crv, public_jwk, encrypted_private_key, key_version, status) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'active')",
            kid, SIGNING_ALG, SIGNING_CRV, json.dumps(public_jwk), ciphertext, version,
        )
        return public_jwk
    except Exception:
        # Lost a provisioning race (one-active unique index rejected us). The
        # winner's key is the truth — re-read and return it. Never silently
        # proceed with our un-persisted private key.
        won = await db.fetchrow(
            "SELECT public_jwk FROM a2a_signing_keys WHERE status = 'active' LIMIT 1")
        if won:
            return _as_dict(won["public_jwk"])
        raise


async def get_active_signing_key(db) -> tuple[str, ec.EllipticCurvePrivateKey]:
    """Load + decrypt the active private key for signing. Raises if none — the
    caller (signing) must never fall back to an unsigned or ad-hoc key."""
    row = await db.fetchrow(
        "SELECT kid, encrypted_private_key, key_version "
        "FROM a2a_signing_keys WHERE status = 'active' LIMIT 1")
    if not row:
        raise RuntimeError(
            "no active A2A signing key — call provision_signing_key() at startup")
    return row["kid"], _decrypt_private(row["encrypted_private_key"], int(row["key_version"]))


async def get_jwks(db) -> dict:
    """Public JWKS: active + retiring keys (public halves only). This is the body
    served at the gateway /.well-known/jwks.json and, via apex rewrite, at
    APEX_JKU. Private material is never read here."""
    rows = await db.fetch(
        "SELECT public_jwk FROM a2a_signing_keys "
        "WHERE status IN ('active', 'retiring') ORDER BY created_at DESC")
    return {"keys": [_as_dict(r["public_jwk"]) for r in rows]}


async def rotate_signing_key(db, keep_retiring: int = 1) -> str:
    """Rotate: generate a new active key, demote the prior active to 'retiring',
    and retire any retiring keys beyond `keep_retiring` (the JWKS overlap window).
    Atomic — the one-active invariant holds throughout. Returns the new kid."""
    kid, private_key, public_jwk = generate_signing_key()
    ciphertext, version = _encrypt_private(private_key)
    async with db.transaction():
        await db.execute(
            "UPDATE a2a_signing_keys SET status = 'retiring' WHERE status = 'active'")
        await db.execute(
            "INSERT INTO a2a_signing_keys "
            "(kid, alg, crv, public_jwk, encrypted_private_key, key_version, status) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, 'active')",
            kid, SIGNING_ALG, SIGNING_CRV, json.dumps(public_jwk), ciphertext, version,
        )
        await db.execute(
            "UPDATE a2a_signing_keys SET status = 'retired', retired_at = NOW() "
            "WHERE status = 'retiring' AND kid NOT IN ("
            "  SELECT kid FROM a2a_signing_keys WHERE status = 'retiring' "
            "  ORDER BY created_at DESC LIMIT $1)",
            keep_retiring,
        )
    return kid
