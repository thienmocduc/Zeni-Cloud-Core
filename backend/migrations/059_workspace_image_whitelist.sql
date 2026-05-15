-- Migration 059 — Per-workspace Docker image registry whitelist
-- Workspace owner self-service add registry prefix (vd: ghcr.io/vietcontech/)
-- Backend _validate_image() check global whitelist + workspace whitelist (opt-in)

CREATE TABLE IF NOT EXISTS workspace_image_whitelist (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  -- Registry prefix (e.g. "ghcr.io/vietcontech/", "registry.gitlab.com/myorg/")
  -- Backend check: image.startswith(prefix) OR normalized.startswith(prefix)
  prefix VARCHAR(255) NOT NULL,
  -- Optional: pull credentials secret_id (for private registries)
  pull_secret_id VARCHAR(120),
  -- Audit
  added_by UUID,
  added_at TIMESTAMPTZ DEFAULT NOW(),
  description TEXT,
  enabled BOOLEAN DEFAULT TRUE,
  UNIQUE(workspace_id, prefix)
);

CREATE INDEX IF NOT EXISTS idx_ws_image_whitelist_ws ON workspace_image_whitelist(workspace_id) WHERE enabled = TRUE;

COMMENT ON TABLE workspace_image_whitelist IS 'Per-workspace Docker registry whitelist. Owner self-service add prefix (vd: ghcr.io/myorg/) để được phép deploy image từ registry đó.';
