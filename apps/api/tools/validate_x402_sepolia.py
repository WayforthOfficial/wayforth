"""validate_x402_sepolia.py — prove the x402 EIP-3009 path on Base Sepolia.

Run this ONCE before flipping WAYFORTH_RAILS_LIVE / WAYFORTH_RAIL_X402 on mainnet.
It exercises the real settlement client (services/x402_client.py) end-to-end on
Base Sepolia — no gateway, no DB, no upstream keys required, because settlement
is a pure on-chain broadcast:

  1. Load the relayer (gas payer / broadcaster) + receiving wallet + payer wallet
     from ~/wayforth-security/*.json (generated during staging setup).
  2. Build + EIP-712-sign a USDC TransferWithAuthorization from the payer to the
     receiving wallet (a small testnet amount).
  3. verify_authorization() — must return valid (signature recovers to payer).
  4. settle_authorization() — relayer broadcasts transferWithAuthorization on
     Base Sepolia; require an on-chain receipt with status == 1.

Preconditions (fund these first — addresses printed by the staging setup):
  * RELAYER  funded with Base Sepolia ETH (pays gas).
  * PAYER    funded with Base Sepolia USDC (the value transferred).

Usage:
    BASE_CHAIN_ID=84532 BASE_RPC=https://sepolia.base.org \
      .venv/bin/python tools/validate_x402_sepolia.py [amount_micro_usdc]
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import x402_client  # noqa: E402

_SEC = os.path.expanduser("~/wayforth-security")


def _load(name: str) -> dict:
    return json.load(open(os.path.join(_SEC, name)))


def main() -> int:
    os.environ.setdefault("BASE_CHAIN_ID", "84532")
    os.environ.setdefault("BASE_RPC", "https://sepolia.base.org")
    # Base Sepolia USDC EIP-712 domain name (Circle testnet token).
    os.environ.setdefault("USDC_EIP712_NAME", "USDC")
    cid = x402_client.chain_id()
    if cid != 84532:
        print(f"refusing: BASE_CHAIN_ID={cid} is not Base Sepolia (84532)")
        return 2

    relayer = _load("x402_sepolia_relayer.json")
    payer = _load("staging_x402_payer.json")
    recv = _load("staging_x402_secrets.json")["WAYFORTH_BASE_WALLET"]
    os.environ["X402_RELAYER_PRIVATE_KEY"] = relayer["private_key"]

    amount_micro = int(sys.argv[1]) if len(sys.argv) > 1 else 1000  # 0.001 USDC default
    now = int(time.time())

    from eth_account import Account
    from eth_account.messages import encode_typed_data

    payer_acct = Account.from_key(payer["private_key"])
    nonce = "0x" + os.urandom(32).hex()
    auth = {
        "from": payer_acct.address,
        "to": recv,
        "value": str(amount_micro),
        "validAfter": str(now - 60),
        "validBefore": str(now + 3600),
        "nonce": nonce,
    }
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
            "from": auth["from"], "to": auth["to"], "value": amount_micro,
            "validAfter": now - 60, "validBefore": now + 3600,
            "nonce": x402_client._nonce_bytes(nonce),
        },
    }
    signed = payer_acct.sign_message(encode_typed_data(full_message=typed))
    signature = signed.signature.hex()
    header = base64.b64encode(json.dumps(
        {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
         "payload": {"signature": signature, "authorization": auth}}
    ).encode()).decode()

    print(f"payer   : {payer_acct.address}")
    print(f"payTo   : {recv}")
    print(f"relayer : {relayer['address']}")
    print(f"amount  : {amount_micro} micro-USDC ({amount_micro/1e6} USDC) on Base Sepolia\n")

    print("[1/2] verify_authorization ...")
    verified = x402_client.verify_authorization(header, recv, amount_micro, cid=cid)
    if not verified.get("valid"):
        print("  ✗ verification FAILED:", verified.get("error"))
        return 1
    print(f"  ✓ valid — signer recovered = {verified['recovered']}")

    print("[2/2] settle_authorization (on-chain broadcast) ...")
    result = x402_client.settle_authorization(verified, signature, cid=cid)
    if not result.get("settled"):
        print("  ✗ settlement FAILED:", result.get("error"))
        print("    (check: relayer funded with Sepolia ETH? payer funded with Sepolia USDC?)")
        return 1
    tx = result["tx_hash"]
    print(f"  ✓ settled — tx={tx}")
    print(f"  https://sepolia.basescan.org/tx/{tx}")
    print("\n✅ x402 EIP-3009 settlement validated on Base Sepolia. Safe to flip the rail on mainnet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
