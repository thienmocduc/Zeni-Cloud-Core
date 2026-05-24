-- Migration 065 — Workspace AI Providers (BYO LLM key per tenant)
-- v151 — chairman CRITICAL item #1 cho WitsAGI deploy full stack.
-- Khách Owner/Admin tự BYO API key — Zeni store metadata, key thật ở Secret Manager.

CREATE TABLE IF NOT EXISTS workspace_ai_providers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  provider VARCHAR(20) NOT NULL,                -- anthropic | deepseek | gemini | openai
  secret_name VARCHAR(120) NOT NULL,            -- Secret Manager name: ws-{ws}-{provider}-key
  set_by UUID,                                  -- user_id who set
  note TEXT,
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (workspace_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_ws_ai_providers ON workspace_ai_providers(workspace_id);

COMMENT ON TABLE workspace_ai_providers IS 'BYO LLM API key per workspace — keys stored in Secret Manager';
