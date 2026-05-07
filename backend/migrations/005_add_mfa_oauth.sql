-- ═══════════════════════════════════════════════════════════
-- Add MFA + OAuth columns to users table
-- Release v4: MFA TOTP + Google OAuth + GitHub OAuth
-- ═══════════════════════════════════════════════════════════

-- OAuth: link user to external provider
ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_provider VARCHAR(16);  -- 'google' | 'github' | NULL (local)
ALTER TABLE users ADD COLUMN IF NOT EXISTS oauth_id       VARCHAR(255); -- provider's user ID (Google sub / GitHub id)

-- MFA: TOTP secret (Fernet-encrypted) + hashed backup codes
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret_enc BYTEA;         -- Fernet(base32 TOTP secret)
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_backup_codes JSONB DEFAULT '[]'::jsonb;  -- list of bcrypt hashes

-- Allow password_hash NULL for OAuth-only users
ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;

-- Unique index: one account per (provider, provider_id)
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth
  ON users(oauth_provider, oauth_id)
  WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL;

-- OAuth state store (prevent CSRF on OAuth callback)
CREATE TABLE IF NOT EXISTS oauth_states (
  state       VARCHAR(64)  PRIMARY KEY,
  provider    VARCHAR(16)  NOT NULL,
  nonce       VARCHAR(64),
  redirect    VARCHAR(255),
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  expires_at  TIMESTAMPTZ  NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_exp ON oauth_states(expires_at);

-- MFA pre-auth token (after password ok, before MFA verify)
CREATE TABLE IF NOT EXISTS mfa_pending (
  token       VARCHAR(64)   PRIMARY KEY,
  user_id     UUID          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  expires_at  TIMESTAMPTZ   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mfa_pending_exp ON mfa_pending(expires_at);

-- Grant privileges on new tables to app user
GRANT ALL PRIVILEGES ON oauth_states TO zeni_app;
GRANT ALL PRIVILEGES ON mfa_pending  TO zeni_app;
