-- ═══════════════════════════════════════════════════════════
-- Workspace-level API tokens (Personal Access Tokens)
-- Cho khách gọi /api/v1/ai/complete + các API khác mà không cần JWT login
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS api_tokens (
  id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name          VARCHAR(128) NOT NULL,
  token_hash    VARCHAR(128) UNIQUE NOT NULL,  -- sha256 of full token
  token_prefix  VARCHAR(20)  NOT NULL,          -- first 12 chars for UI display: zeni_pat_xxx
  scopes        VARCHAR(255) NOT NULL DEFAULT 'ai',  -- 'ai' | 'full' | 'ai,data,web3'
  created_by    UUID         REFERENCES users(id) ON DELETE SET NULL,
  last_used_at  TIMESTAMPTZ,
  use_count     INTEGER      NOT NULL DEFAULT 0,
  expires_at    TIMESTAMPTZ,                    -- null = never expire
  revoked       BOOLEAN      NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_ws        ON api_tokens(workspace_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash      ON api_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_tokens_active    ON api_tokens(workspace_id, revoked) WHERE NOT revoked;

GRANT ALL PRIVILEGES ON api_tokens TO zeni_app;
