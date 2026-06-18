"""core/agent_secrets.py — AES-256-GCM encryption for per-agent env vars.

Secrets are encrypted before writing to hosted_agents.env_encrypted and
decrypted only at dispatch time, injected into the sandbox as env vars,
and never logged or written back to any column.

Key: AGENT_SECRET_KEY env var (32 bytes hex, i.e. 64 hex chars).
     Generate with: python -c "import secrets; print(secrets.token_hex(32))"

CLOUD-7 (2026-06): fail CLOSED on a missing/invalid key. The previous code
silently substituted an all-zero key (`b"\\x00" * 32`) when AGENT_SECRET_KEY was
absent or malformed — which would encrypt every agent secret under a
publicly-known key. There is now NO zero-key fallback:
  * in production (ENVIRONMENT=production) a bad key raises at import → the app
    cannot start without a real key;
  * elsewhere the cipher is simply not built, and any encrypt/decrypt attempt
    raises loudly rather than using a weak key.
"""
from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12

_KEY_HEX = os.environ.get("AGENT_SECRET_KEY", "")
_IS_PRODUCTION = os.environ.get("ENVIRONMENT", "development").lower() == "production"


def _valid_key(hex_str: str) -> bool:
    """True only for an exact 32-byte (64 hex char) key."""
    if len(hex_str) != 64:
        return False
    try:
        bytes.fromhex(hex_str)
        return True
    except ValueError:
        return False


if _valid_key(_KEY_HEX):
    _GCM: AESGCM | None = AESGCM(bytes.fromhex(_KEY_HEX))
    _KEY_ERROR: str | None = None
else:
    # NO all-zero fallback. Fail closed.
    _GCM = None
    _KEY_ERROR = (
        "AGENT_SECRET_KEY is missing or not a 64-hex-character (32-byte) value. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\" "
        "and set it in the deploy environment. Agent-secret encryption is disabled "
        "until a valid key is provided."
    )
    if _IS_PRODUCTION:
        # Production must never run without real agent-secret encryption.
        raise RuntimeError(_KEY_ERROR)


def _require_cipher() -> AESGCM:
    """Return the AES-GCM cipher, or raise if no valid key is configured.

    This is the single choke point that guarantees we never encrypt or decrypt
    agent secrets under a weak/absent key (CLOUD-7)."""
    if _GCM is None:
        raise RuntimeError(_KEY_ERROR or "AGENT_SECRET_KEY is not configured")
    return _GCM


def encrypt_env(env: dict[str, str]) -> bytes:
    """Encrypt a dict of env vars to bytes (nonce || ciphertext).

    Raises RuntimeError if no valid AGENT_SECRET_KEY is configured — we never
    fall back to a weak key (CLOUD-7)."""
    import secrets as _sec
    gcm = _require_cipher()
    nonce = _sec.token_bytes(_NONCE_LEN)
    ct = gcm.encrypt(nonce, json.dumps(env).encode(), None)
    return nonce + ct


def decrypt_env(data: bytes) -> dict[str, str]:
    """Decrypt env bytes back to dict. Returns {} on malformed ciphertext.

    Raises RuntimeError if no valid AGENT_SECRET_KEY is configured — a missing
    key is a misconfiguration to surface loudly, not silently swallow (CLOUD-7).
    Genuine decrypt failures (corrupt/forged ciphertext) still return {}."""
    gcm = _require_cipher()
    if not data or len(data) <= _NONCE_LEN:
        return {}
    try:
        nonce, ct = data[:_NONCE_LEN], data[_NONCE_LEN:]
        plain = gcm.decrypt(nonce, ct, None)
        return json.loads(plain)
    except Exception:
        return {}
