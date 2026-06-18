"""services/x402_client.py — EIP-3009 verification + on-chain settlement for x402.

This is the real settlement layer the x402 rail was waiting on (FINDING-001).
The old `_verify_x402_payment` only parsed client JSON and checked the payee +
amount fields — a forged envelope "verified", so the rail was hard-disabled. The
fix is in `verify_authorization()`: it recovers the EIP-712 signature over the
USDC `TransferWithAuthorization` typed data and rejects anything whose signer is
not the `from` address. A forged envelope cannot produce a valid signature, so
it fails closed. THIS is what makes flipping the x402 rail safe.

Settlement (`settle_authorization`) submits the signed authorization on-chain via
`transferWithAuthorization` — the payer signed an off-chain authorization; a
funded relayer/CDP facilitator broadcasts it and pays gas. We require an on-chain
receipt with status == 1 before treating the call as paid.

Live gating (`x402_settlement_ready`) requires the CDP signing keys AND the
Wayforth Base wallet to be configured. core.rails.rail_live("x402") additionally
requires the launch flag. Both must hold before any real money moves.

EIP-3009 reference: USDC implements `transferWithAuthorization` (EIP-3009). The
payer signs typed data; the contract verifies the signature on-chain and moves
`value` from `from` to `to`, single-use per `nonce`, valid only within
[validAfter, validBefore].
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger("wayforth")

# ── Network / asset constants ─────────────────────────────────────────────────
# USDC is deployed per-chain; the EIP-712 domain (name/version/verifyingContract)
# must match the exact contract the payer signed against or recovery yields the
# wrong signer and verification fails closed.
_USDC_BY_CHAIN: dict[int, str] = {
    8453:  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # Base mainnet
    84532: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",   # Base Sepolia
}
# Circle's USDC EIP-712 domain name. Overridable per-deploy in case a testnet
# token uses a different name; default matches Base mainnet native USDC.
_USDC_DOMAIN_NAME = os.environ.get("USDC_EIP712_NAME", "USD Coin")
_USDC_DOMAIN_VERSION = os.environ.get("USDC_EIP712_VERSION", "2")

_TRANSFER_WITH_AUTH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

# Minimal USDC ABI fragment — only transferWithAuthorization(v,r,s overload).
_USDC_ABI = [
    {
        "name": "transferWithAuthorization",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
]


def chain_id() -> int:
    try:
        return int(os.environ.get("BASE_CHAIN_ID", "8453"))
    except ValueError:
        return 8453


def usdc_address(cid: int | None = None) -> str:
    cid = cid if cid is not None else chain_id()
    return _USDC_BY_CHAIN.get(cid, _USDC_BY_CHAIN[8453])


def x402_settlement_ready() -> bool:
    """True iff real on-chain settlement can actually run.

    Requires the CDP signing keys (or a relayer private key) AND the Wayforth
    Base receiving wallet. This is the hard gate core.rails consults so the rail
    can never be 'live' without a funded settlement path behind it.
    """
    base_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    cdp_ok = bool(os.environ.get("CDP_API_KEY_NAME")) and bool(
        os.environ.get("CDP_API_KEY_PRIVATE_KEY")
    )
    relayer_ok = bool(os.environ.get("X402_RELAYER_PRIVATE_KEY"))
    return bool(base_wallet) and (cdp_ok or relayer_ok)


# ── Authorization parsing ─────────────────────────────────────────────────────


def parse_payment_header(payment_header: str) -> dict | None:
    """Decode a base64 X-PAYMENT header into the authorization + signature.

    Accepts both the canonical x402 'exact' shape
        {"payload": {"signature": "0x..", "authorization": {from,to,value,...}}}
    and a legacy flat shape
        {"from": .., "to": .., "value": .., "signature": "0x.."}.
    Returns {"authorization": {...}, "signature": "0x.."} or None if undecodable.
    """
    import base64
    import json

    try:
        raw = base64.b64decode(payment_header + "==", validate=False)
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("x402 header decode failed: %s", exc)
        return None
    if not isinstance(decoded, dict):
        return None

    payload = decoded.get("payload") if isinstance(decoded.get("payload"), dict) else decoded
    auth = payload.get("authorization")
    if not isinstance(auth, dict):
        # legacy flat shape: the envelope itself carries the fields
        auth = {
            k: payload.get(k)
            for k in ("from", "to", "value", "validAfter", "validBefore", "nonce")
            if k in payload
        }
    signature = payload.get("signature") or decoded.get("signature")
    if not auth or not signature:
        return None
    return {"authorization": auth, "signature": signature}


def _to_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def _nonce_bytes(nonce: Any) -> bytes:
    if isinstance(nonce, (bytes, bytearray)):
        return bytes(nonce)
    s = str(nonce)
    if s.startswith("0x"):
        s = s[2:]
    b = bytes.fromhex(s.rjust(64, "0"))
    return b[-32:].rjust(32, b"\x00")


# ── Verification (the security-critical core) ─────────────────────────────────


def verify_authorization(
    payment_header: str,
    expected_to: str,
    expected_value_micro: int,
    *,
    cid: int | None = None,
    now: int | None = None,
) -> dict:
    """Recover + validate an EIP-3009 authorization. Fails closed.

    Returns a dict with `valid` plus diagnostic fields. `valid` is True ONLY when:
      - the header decodes to an authorization + signature,
      - the EIP-712 signature recovers to exactly the `from` address,
      - `to` matches the expected payee (our wallet),
      - `value` >= expected (within 0.5% tolerance),
      - the current time is within [validAfter, validBefore].
    A forged or tampered envelope cannot satisfy the signature check.
    """
    cid = cid if cid is not None else chain_id()
    now = now if now is not None else int(time.time())

    parsed = parse_payment_header(payment_header)
    if not parsed:
        return {"valid": False, "error": "decode_failed", "from_address": None}

    auth = parsed["authorization"]
    signature = parsed["signature"]
    from_address = (auth.get("from") or "").lower()
    to_address = (auth.get("to") or "").lower()
    value = _to_int(auth.get("value"))
    valid_after = _to_int(auth.get("validAfter"))
    valid_before = _to_int(auth.get("validBefore"))

    # 1. Signature recovery — the check that makes forgery impossible.
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        typed = {
            "types": _TRANSFER_WITH_AUTH_TYPES,
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": _USDC_DOMAIN_NAME,
                "version": _USDC_DOMAIN_VERSION,
                "chainId": cid,
                "verifyingContract": usdc_address(cid),
            },
            "message": {
                "from": auth.get("from"),
                "to": auth.get("to"),
                "value": value,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": _nonce_bytes(auth.get("nonce")),
            },
        }
        signable = encode_typed_data(full_message=typed)
        recovered = Account.recover_message(signable, signature=signature).lower()
    except Exception as exc:
        logger.warning("x402 signature recovery failed: %s", exc)
        return {"valid": False, "error": "signature_recovery_failed", "from_address": from_address}

    if not from_address or recovered != from_address:
        logger.warning("x402 signer mismatch: recovered=%s from=%s", recovered, from_address)
        return {
            "valid": False,
            "error": "signer_mismatch",
            "from_address": from_address,
            "recovered": recovered,
        }

    # 2. Payee must be our wallet — never settle a payment routed elsewhere.
    if expected_to and to_address and to_address != expected_to.lower():
        return {"valid": False, "error": "payee_mismatch", "from_address": from_address,
                "to_address": to_address}

    # 3. Time window.
    if valid_after and now < valid_after:
        return {"valid": False, "error": "not_yet_valid", "from_address": from_address}
    if valid_before and now >= valid_before:
        return {"valid": False, "error": "authorization_expired", "from_address": from_address}

    # 4. Amount — 0.5% tolerance, matching the rail's underpayment policy.
    within_tolerance = value >= int(expected_value_micro * 0.995)

    return {
        "valid": bool(within_tolerance),
        "from_address": from_address,
        "to_address": to_address,
        "recovered": recovered,
        "received_micro": value,
        "expected_micro": int(expected_value_micro),
        "valid_after": valid_after,
        "valid_before": valid_before,
        "nonce": auth.get("nonce"),
        "error": None if within_tolerance else "underpaid",
    }


# ── Settlement (on-chain broadcast; needs a funded relayer/CDP) ────────────────


def _split_signature(signature: str) -> tuple[int, bytes, bytes]:
    sig = signature[2:] if signature.startswith("0x") else signature
    sig_bytes = bytes.fromhex(sig)
    if len(sig_bytes) != 65:
        raise ValueError(f"signature must be 65 bytes, got {len(sig_bytes)}")
    r = sig_bytes[0:32]
    s = sig_bytes[32:64]
    v = sig_bytes[64]
    if v < 27:
        v += 27
    return v, r, s


def settle_authorization(verified: dict, signature: str, *, cid: int | None = None) -> dict:
    """Broadcast `transferWithAuthorization` on-chain via a funded relayer.

    `verified` is the dict returned by verify_authorization (must be valid).
    Returns {"settled": bool, "tx_hash": str|None, "error": str|None}. Requires
    a relayer private key (X402_RELAYER_PRIVATE_KEY); the CDP server-wallet path
    is used when relayer key is absent but CDP is configured.

    NB: this moves real USDC. It is unreachable unless core.rails.rail_live(
    "x402") is True, which requires both the launch flag and settlement-ready.
    Validate end-to-end on Base Sepolia (BASE_CHAIN_ID=84532) before mainnet.
    """
    cid = cid if cid is not None else chain_id()
    relayer_key = os.environ.get("X402_RELAYER_PRIVATE_KEY", "")
    rpc_url = os.environ.get("BASE_RPC", "")

    if not verified.get("valid"):
        return {"settled": False, "tx_hash": None, "error": "authorization_not_valid"}

    # Prefer a direct web3 relayer when configured (testable, no SaaS dependency).
    if relayer_key and rpc_url:
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(rpc_url))
            acct = w3.eth.account.from_key(relayer_key)
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(usdc_address(cid)), abi=_USDC_ABI
            )
            v, r, s = _split_signature(signature)
            fn = usdc.functions.transferWithAuthorization(
                Web3.to_checksum_address(verified["from_address"]),
                Web3.to_checksum_address(verified["to_address"]),
                int(verified["received_micro"]),
                int(verified.get("valid_after") or 0),
                int(verified.get("valid_before") or 0),
                _nonce_bytes(verified.get("nonce")),
                v, r, s,
            )
            tx = fn.build_transaction({
                "from": acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "chainId": cid,
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.get("status") != 1:
                return {"settled": False, "tx_hash": tx_hash.hex(), "error": "receipt_status_0"}
            return {"settled": True, "tx_hash": tx_hash.hex(), "error": None}
        except Exception as exc:
            logger.error("x402 relayer settlement failed: %s", exc)
            return {"settled": False, "tx_hash": None, "error": f"relayer_error:{exc}"[:200]}

    # CDP server-wallet path: invoke the same contract method through CDP.
    cdp_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_pk = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    if cdp_name and cdp_pk:
        try:
            from cdp import Cdp, Wallet

            Cdp.configure(cdp_name, cdp_pk)
            wallet = Wallet.fetch(os.environ.get("WAYFORTH_BASE_WALLET", ""))
            v, r, s = _split_signature(signature)
            invocation = wallet.invoke_contract(
                contract_address=usdc_address(cid),
                method="transferWithAuthorization",
                abi=_USDC_ABI,
                args={
                    "from": verified["from_address"],
                    "to": verified["to_address"],
                    "value": str(verified["received_micro"]),
                    "validAfter": str(verified.get("valid_after") or 0),
                    "validBefore": str(verified.get("valid_before") or 0),
                    "nonce": verified.get("nonce"),
                    "v": v,
                    "r": "0x" + r.hex(),
                    "s": "0x" + s.hex(),
                },
            )
            invocation.wait(timeout_seconds=120, interval_seconds=2)
            return {"settled": True, "tx_hash": invocation.transaction_hash, "error": None}
        except Exception as exc:
            logger.error("x402 CDP settlement failed: %s", exc)
            return {"settled": False, "tx_hash": None, "error": f"cdp_error:{exc}"[:200]}

    return {"settled": False, "tx_hash": None, "error": "no_settlement_path_configured"}
