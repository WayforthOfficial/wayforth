-- 065: A2A Agent Card signing keys (Option C — encrypted-in-DB EC P-256).
--
-- The gateway signs its Agent Card (JWS / ES256) with the ACTIVE key. The PUBLIC
-- halves of all active+retiring keys are published as a JWKS so a verifier can
-- check the card signature. We keep >=2 keys in the JWKS during a rotation
-- (one 'active', >=1 'retiring') so a verifier that cached the old key mid-
-- rotation still validates — no coordinated flag day.
--
-- Private keys are Fernet-encrypted with the SAME versioned layer as api_keys
-- (core/auth.encrypt_api_key), never stored in plaintext, and never leave the
-- gateway. The gateway is the single source of truth for the keypair; the apex
-- (wayforth.io/.well-known/jwks.json) transparently rewrites to the gateway's
-- JWKS, so a rotation needs no apex/Lovable deploy.

CREATE TABLE IF NOT EXISTS a2a_signing_keys (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kid                   TEXT NOT NULL UNIQUE,        -- RFC 7638 JWK thumbprint; in JWKS + JWS header
  alg                   TEXT NOT NULL DEFAULT 'ES256',
  crv                   TEXT NOT NULL DEFAULT 'P-256',
  public_jwk            JSONB NOT NULL,              -- {kty,crv,x,y,kid,use,alg} — served in JWKS
  encrypted_private_key TEXT NOT NULL,               -- Fernet-encrypted PKCS8 PEM (never plaintext)
  key_version           INTEGER NOT NULL DEFAULT 1,  -- which ENCRYPTION_KEY version encrypted it
  status                TEXT NOT NULL DEFAULT 'active', -- 'active' | 'retiring' | 'retired'
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  activated_at          TIMESTAMPTZ DEFAULT NOW(),
  retired_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_a2a_signing_keys_status
  ON a2a_signing_keys (status, created_at DESC);

-- Invariant: at most ONE active signing key at any time. A racing second
-- provision/rotation is rejected by this partial unique index, not silently
-- accepted — keys.py catches the violation and re-reads the winner.
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2a_signing_keys_one_active
  ON a2a_signing_keys (status) WHERE status = 'active';
