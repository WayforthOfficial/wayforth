"""test_a2a_keys.py — Option-C signing keys + signed Agent Card (target 0.9.2).

Security-critical layer. Covers the crypto (EC P-256 / ES256 / JWK thumbprint),
the encrypted-in-DB private-key roundtrip, the sign→verify path with tamper +
wrong-key + unknown-kid rejection, and the rotation invariants (one active key,
>=2-key JWKS overlap window, public-only JWKS).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.fernet import Fernet

import core.auth as auth
from core.a2a import keys as K
from core.a2a import sign as SIGN
from core.a2a.card import build_agent_card


@pytest.fixture(autouse=True)
def _fernet_env(monkeypatch):
    # Provision a real v1 Fernet key so encrypt/decrypt of the private key runs
    # the genuine path (no stubbing of the crypto under test).
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(auth, "KEY_VERSIONS", {}, raising=False)
    yield
    monkeypatch.setattr(auth, "KEY_VERSIONS", {}, raising=False)


_SKILLS = [{"id": "echo", "name": "Echo", "description": "d", "tags": ["x"]}]


def _card():
    return build_agent_card(name="WF", description="d",
                            url="https://gateway.wayforth.io/a2a", version="0.9.2",
                            skills=_SKILLS)


# ── in-memory DB modelling just the a2a_signing_keys queries ──────────────────

class FakeKeyDB:
    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    async def fetchrow(self, q, *a):
        if "status = 'active'" in q:
            return next((r for r in self.rows if r["status"] == "active"), None)
        return None

    async def fetch(self, q, *a):
        if "IN ('active', 'retiring')" in q:
            sel = [r for r in self.rows if r["status"] in ("active", "retiring")]
            return sorted(sel, key=lambda r: r["created_at"], reverse=True)
        return []

    async def execute(self, q, *a):
        if "INSERT INTO a2a_signing_keys" in q:
            kid, alg, crv, pj, ct, ver = a[:6]
            self.rows.append({
                "kid": kid, "alg": alg, "crv": crv, "public_jwk": json.loads(pj),
                "encrypted_private_key": ct, "key_version": ver,
                "status": "active", "created_at": self._next(),
            })
        elif "SET status = 'retiring' WHERE status = 'active'" in q:
            for r in self.rows:
                if r["status"] == "active":
                    r["status"] = "retiring"
        elif "SET status = 'retired'" in q:
            keep = a[0]
            retiring = sorted([r for r in self.rows if r["status"] == "retiring"],
                              key=lambda r: r["created_at"], reverse=True)
            for r in retiring[keep:]:
                r["status"] = "retired"

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *exc):
                return False
        return _Tx()

    # test helpers
    def active(self):
        return [r for r in self.rows if r["status"] == "active"]


# ── crypto primitives ─────────────────────────────────────────────────────────

def test_generate_key_kid_is_thumbprint_and_jwk_well_formed():
    kid, priv, jwk = K.generate_signing_key()
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    assert jwk["use"] == "sig" and jwk["alg"] == "ES256"
    assert jwk["kid"] == kid == K.jwk_thumbprint(jwk)   # content-addressed kid
    assert "d" not in jwk                                # never the private scalar


def test_private_key_encrypt_decrypt_roundtrip():
    _, priv, _ = K.generate_signing_key()
    ct, ver = K._encrypt_private(priv)
    assert ver == 1 and isinstance(ct, str)
    restored = K._decrypt_private(ct, ver)
    # Same key → same public numbers.
    assert restored.public_key().public_numbers() == priv.public_key().public_numbers()


# ── sign / verify (security) ──────────────────────────────────────────────────

def test_sign_then_verify_roundtrip():
    kid, priv, jwk = K.generate_signing_key()
    signed = SIGN.sign_card(_card(), kid, priv)
    assert signed["signatures"] and signed["signatures"][0]["protected"]
    assert SIGN.verify_card(signed, {"keys": [jwk]}) is True


def test_tampered_card_fails_verification():
    kid, priv, jwk = K.generate_signing_key()
    signed = SIGN.sign_card(_card(), kid, priv)
    signed["description"] = "tampered after signing"   # any covered field
    assert SIGN.verify_card(signed, {"keys": [jwk]}) is False


def test_wrong_key_fails_verification():
    kid, priv, _ = K.generate_signing_key()
    _, _, other_jwk = K.generate_signing_key()
    signed = SIGN.sign_card(_card(), kid, priv)
    assert SIGN.verify_card(signed, {"keys": [other_jwk]}) is False


def test_unknown_kid_and_unsigned_fail():
    kid, priv, jwk = K.generate_signing_key()
    signed = SIGN.sign_card(_card(), kid, priv)
    assert SIGN.verify_card(signed, {"keys": []}) is False        # kid not in JWKS
    assert SIGN.verify_card(_card(), {"keys": [jwk]}) is False     # no signatures


def test_signature_header_carries_apex_jku():
    kid, priv, _ = K.generate_signing_key()
    signed = SIGN.sign_card(_card(), kid, priv)
    header = json.loads(SIGN._b64url_decode(signed["signatures"][0]["protected"]))
    assert header["alg"] == "ES256" and header["kid"] == kid
    assert header["jku"] == "https://wayforth.io/.well-known/jwks.json"


# ── DB lifecycle: provision / rotate / jwks invariants ────────────────────────

def test_provision_is_idempotent_one_active():
    db = FakeKeyDB()
    jwk1 = asyncio.run(K.provision_signing_key(db))
    jwk2 = asyncio.run(K.provision_signing_key(db))
    assert jwk1["kid"] == jwk2["kid"]      # second call is a no-op
    assert len(db.active()) == 1


def test_get_active_decrypts_usable_key():
    db = FakeKeyDB()
    pub = asyncio.run(K.provision_signing_key(db))
    kid, priv = asyncio.run(K.get_active_signing_key(db))
    assert kid == pub["kid"]
    # The decrypted key actually signs a card that verifies against the public JWK.
    signed = SIGN.sign_card(_card(), kid, priv)
    assert SIGN.verify_card(signed, {"keys": [pub]}) is True


def test_jwks_is_public_only():
    db = FakeKeyDB()
    asyncio.run(K.provision_signing_key(db))
    jwks = asyncio.run(K.get_jwks(db))
    assert len(jwks["keys"]) == 1
    for k in jwks["keys"]:
        assert "encrypted_private_key" not in k and "d" not in k


def test_rotation_keeps_two_key_overlap_then_retires():
    db = FakeKeyDB()
    asyncio.run(K.provision_signing_key(db))
    k1 = db.active()[0]["kid"]

    k2 = asyncio.run(K.rotate_signing_key(db, keep_retiring=1))
    assert len(db.active()) == 1 and db.active()[0]["kid"] == k2
    jwks = asyncio.run(K.get_jwks(db))
    kids = {k["kid"] for k in jwks["keys"]}
    assert kids == {k1, k2}                 # >=2 overlap: old key still verifiable

    k3 = asyncio.run(K.rotate_signing_key(db, keep_retiring=1))
    jwks = asyncio.run(K.get_jwks(db))
    kids = {k["kid"] for k in jwks["keys"]}
    assert kids == {k2, k3}                 # window held at 2; k1 retired (dropped)
    assert k1 not in kids
    assert len(db.active()) == 1            # one-active invariant preserved


def test_build_signed_card_end_to_end():
    db = FakeKeyDB()
    pub = asyncio.run(K.provision_signing_key(db))
    signed = asyncio.run(SIGN.build_signed_card(
        db, name="WF", description="d", url="https://gateway.wayforth.io/a2a",
        version="0.9.2", skills=_SKILLS))
    assert signed["protocolVersion"] == "0.3.0"
    assert SIGN.verify_card(signed, {"keys": [pub]}) is True
