"""core/agent_secrets.py — AES-256-GCM encryption for per-agent env vars.

Secrets are encrypted before writing to hosted_agents.env_encrypted and
decrypted only at dispatch time, injected into the sandbox as env vars,
and never logged or written back to any column.

Key: E2B_SECRET_KEY env var (32 bytes hex, i.e. 64 hex chars).
     Generate with: python -c "import secrets; print(secrets.token_hex(32))"
"""
from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_HEX = os.environ.get("AGENT_SECRET_KEY", "")
_KEY_BYTES = bytes.fromhex(_KEY_HEX) if len(_KEY_HEX) == 64 else b"\x00" * 32
_GCM = AESGCM(_KEY_BYTES)
_NONCE_LEN = 12


def encrypt_env(env: dict[str, str]) -> bytes:
    """Encrypt a dict of env vars to bytes (nonce || ciphertext)."""
    import secrets as _sec
    nonce = _sec.token_bytes(_NONCE_LEN)
    ct = _GCM.encrypt(nonce, json.dumps(env).encode(), None)
    return nonce + ct


def decrypt_env(data: bytes) -> dict[str, str]:
    """Decrypt env bytes back to dict. Returns {} on error."""
    if not data or len(data) <= _NONCE_LEN:
        return {}
    try:
        nonce, ct = data[:_NONCE_LEN], data[_NONCE_LEN:]
        plain = _GCM.decrypt(nonce, ct, None)
        return json.loads(plain)
    except Exception:
        return {}
