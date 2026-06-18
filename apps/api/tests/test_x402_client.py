"""test_x402_client.py — EIP-3009 verification closes the forged-envelope hole.

The whole reason the x402 rail was disabled (FINDING-001): the old verifier only
parsed client JSON, so a forged X-PAYMENT envelope "verified" and bought free
service. These tests prove the new verifier recovers the EIP-712 signature and
rejects anything not signed by `from` — without touching a chain.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from services import x402_client

_CHAIN = 84532  # Base Sepolia for the test domain
_PAYEE = "0x000000000000000000000000000000000000bEEF"


def _sign_auth(from_priv, to_addr, value, valid_after, valid_before, nonce_hex, cid=_CHAIN):
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    acct = Account.from_key(from_priv)
    typed = {
        "types": x402_client._TRANSFER_WITH_AUTH_TYPES,
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": x402_client._USDC_DOMAIN_NAME,
            "version": x402_client._USDC_DOMAIN_VERSION,
            "chainId": cid,
            "verifyingContract": x402_client.usdc_address(cid),
        },
        "message": {
            "from": acct.address,
            "to": to_addr,
            "value": value,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": x402_client._nonce_bytes(nonce_hex),
        },
    }
    signable = encode_typed_data(full_message=typed)
    signed = acct.sign_message(signable)
    return acct.address, signed.signature.hex()


def _header(auth: dict, signature: str) -> str:
    env = {"x402Version": 1, "scheme": "exact", "network": "base",
           "payload": {"signature": signature, "authorization": auth}}
    return base64.b64encode(json.dumps(env).encode()).decode()


def _good_auth(from_addr, value=3000, span=600):
    now = int(time.time())
    return {
        "from": from_addr,
        "to": _PAYEE,
        "value": str(value),
        "validAfter": str(now - 60),
        "validBefore": str(now + span),
        "nonce": "0x" + ("11" * 32),
    }


def test_genuine_signature_verifies():
    from eth_account import Account
    acct = Account.create()
    addr, sig = _sign_auth(acct.key, _PAYEE, 3000,
                           int(time.time()) - 60, int(time.time()) + 600, "0x" + "11" * 32)
    auth = _good_auth(addr)
    res = x402_client.verify_authorization(_header(auth, sig), _PAYEE, 3000, cid=_CHAIN)
    assert res["valid"] is True
    assert res["from_address"] == addr.lower()
    assert res["recovered"] == addr.lower()


def test_forged_envelope_fails():
    """No signature can be produced for a wallet you don't control."""
    forged_auth = {
        "from": "0x000000000000000000000000000000000000dEaD",
        "to": _PAYEE,
        "value": "3000",
        "validAfter": str(int(time.time()) - 60),
        "validBefore": str(int(time.time()) + 600),
        "nonce": "0x" + "22" * 32,
    }
    # A garbage 65-byte signature recovers to *some* address, never `from`.
    fake_sig = "0x" + ("ab" * 64) + "1b"
    res = x402_client.verify_authorization(_header(forged_auth, fake_sig), _PAYEE, 3000, cid=_CHAIN)
    assert res["valid"] is False
    assert res["error"] in ("signer_mismatch", "signature_recovery_failed")


def test_tampered_amount_fails():
    """Sign for 3000 micro-USDC, then inflate value in the envelope → signer mismatch."""
    from eth_account import Account
    acct = Account.create()
    now = int(time.time())
    addr, sig = _sign_auth(acct.key, _PAYEE, 3000, now - 60, now + 600, "0x" + "33" * 32)
    auth = _good_auth(addr, value=3000)
    auth["value"] = "9000"  # tamper after signing
    res = x402_client.verify_authorization(_header(auth, sig), _PAYEE, 9000, cid=_CHAIN)
    assert res["valid"] is False
    assert res["error"] == "signer_mismatch"


def test_payee_mismatch_fails():
    from eth_account import Account
    acct = Account.create()
    now = int(time.time())
    other_payee = "0x0000000000000000000000000000000000001234"
    addr, sig = _sign_auth(acct.key, other_payee, 3000, now - 60, now + 600, "0x" + "44" * 32)
    auth = {"from": addr, "to": other_payee, "value": "3000",
            "validAfter": str(now - 60), "validBefore": str(now + 600), "nonce": "0x" + "44" * 32}
    res = x402_client.verify_authorization(_header(auth, sig), _PAYEE, 3000, cid=_CHAIN)
    assert res["valid"] is False
    assert res["error"] == "payee_mismatch"


def test_expired_authorization_fails():
    from eth_account import Account
    acct = Account.create()
    now = int(time.time())
    addr, sig = _sign_auth(acct.key, _PAYEE, 3000, now - 600, now - 10, "0x" + "55" * 32)
    auth = {"from": addr, "to": _PAYEE, "value": "3000",
            "validAfter": str(now - 600), "validBefore": str(now - 10), "nonce": "0x" + "55" * 32}
    res = x402_client.verify_authorization(_header(auth, sig), _PAYEE, 3000, cid=_CHAIN)
    assert res["valid"] is False
    assert res["error"] == "authorization_expired"


def test_underpaid_fails():
    from eth_account import Account
    acct = Account.create()
    now = int(time.time())
    addr, sig = _sign_auth(acct.key, _PAYEE, 1000, now - 60, now + 600, "0x" + "66" * 32)
    auth = {"from": addr, "to": _PAYEE, "value": "1000",
            "validAfter": str(now - 60), "validBefore": str(now + 600), "nonce": "0x" + "66" * 32}
    res = x402_client.verify_authorization(_header(auth, sig), _PAYEE, 3000, cid=_CHAIN)
    assert res["valid"] is False
    assert res["error"] == "underpaid"


def test_undecodable_header_fails_closed():
    res = x402_client.verify_authorization("!!!not-base64!!!", _PAYEE, 3000, cid=_CHAIN)
    assert res["valid"] is False
    assert res["error"] in ("decode_failed", "signature_recovery_failed")


def test_settlement_not_ready_without_config(monkeypatch):
    for var in ("WAYFORTH_BASE_WALLET", "CDP_API_KEY_NAME", "CDP_API_KEY_PRIVATE_KEY",
                "X402_RELAYER_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert x402_client.x402_settlement_ready() is False


def test_settlement_ready_with_relayer(monkeypatch):
    monkeypatch.setenv("WAYFORTH_BASE_WALLET", _PAYEE)
    monkeypatch.setenv("X402_RELAYER_PRIVATE_KEY", "0x" + "11" * 32)
    assert x402_client.x402_settlement_ready() is True
