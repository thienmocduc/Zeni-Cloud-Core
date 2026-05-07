-- ============================================================================
-- Migration 021 — ZeniRouter (smart multi-model AI router)
-- Adds: per-tenant cost ceiling, usage logging, exact-match cache layer.
-- ============================================================================

-- Per-tenant monthly cost ceiling (enforced before each call)
CREATE TABLE IF NOT EXISTS router_tenant_quotas (
    workspace_id VARCHAR(32) PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    monthly_quota_usd NUMERIC(10,2) NOT NULL DEFAULT 5.00,
    current_month_usage_usd NUMERIC(10,4) NOT NULL DEFAULT 0.0,
    quota_reset_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Per-call usage log (for analytics + dashboards)
CREATE TABLE IF NOT EXISTS router_usage_log (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_email TEXT,
    product TEXT,
    task_type TEXT,
    primary_model TEXT NOT NULL,
    served_by_model TEXT,
    tier TEXT,
    input_tokens INT,
    output_tokens INT,
    cost_usd NUMERIC(10,6),
    latency_ms INT,
    cache_hit BOOLEAN DEFAULT FALSE,
    failover_count INT DEFAULT 0,
    decision_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_router_usage_workspace ON router_usage_log(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_router_usage_model ON router_usage_log(primary_model, created_at DESC);

-- Exact-match cache (SHA256 of tenant_id + messages + model + temp)
CREATE TABLE IF NOT EXISTS router_cache (
    cache_key VARCHAR(64) PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    response_text TEXT NOT NULL,
    model_id TEXT,
    input_tokens INT,
    output_tokens INT,
    hit_count INT DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_router_cache_expires ON router_cache(expires_at);

-- Default quota for every existing workspace
INSERT INTO router_tenant_quotas (workspace_id, monthly_quota_usd)
SELECT id, 5.00 FROM workspaces
ON CONFLICT (workspace_id) DO NOTHING;
