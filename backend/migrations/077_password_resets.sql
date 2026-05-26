-- ============================================================================
-- Migration 077 — Password reset tokens
--
-- Single-use tokens emailed via /auth/password/forgot/init.
-- Stored as SHA-256 hash (token text only sent in email body, never persisted).
-- TTL: 1 hour. used_at set on consumption.
-- ============================================================================

CREATE TABLE IF NOT EXISTS password_resets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   VARCHAR(64) NOT NULL,        -- SHA-256 hex digest of token
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_hash      VARCHAR(64)                  -- SHA-256(client_ip)[:64], for forensics
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_password_resets_token_hash
    ON password_resets(token_hash);
CREATE INDEX IF NOT EXISTS idx_password_resets_user_pending
    ON password_resets(user_id, used_at) WHERE used_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_password_resets_expires
    ON password_resets(expires_at) WHERE used_at IS NULL;

-- Janitor: tokens older than 24h can be safely deleted (cron-friendly query)
-- DELETE FROM password_resets WHERE created_at < NOW() - INTERVAL '24 hours';
