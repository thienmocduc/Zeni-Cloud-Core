-- ============================================================================
-- Migration 020 — Pricing tiers + workspace subscriptions + usage counters
-- 5 tiers: Free / Starter / Pro / Business / Enterprise
-- Drops billing payment integration (manual-payment / Zeni Holdings internal use).
-- ============================================================================

-- Pricing plans (locked tiers)
CREATE TABLE IF NOT EXISTS pricing_plans (
    id VARCHAR(32) PRIMARY KEY,           -- 'free','starter','pro','business','enterprise'
    name VARCHAR(60) NOT NULL,
    price_vnd_monthly INT NOT NULL,
    price_usd_monthly NUMERIC(10,2) NOT NULL,
    quota_requests_per_month BIGINT NOT NULL,
    quota_ai_tokens_per_month BIGINT NOT NULL,
    quota_storage_gb INT NOT NULL,
    quota_router_usd_per_month NUMERIC(10,2) NOT NULL,
    quota_projects INT NOT NULL,             -- max projects (apps); -1 = unlimited
    quota_dev_seats INT NOT NULL,
    sla_uptime_percent NUMERIC(5,3),         -- 99.9 etc.
    support_level VARCHAR(20),               -- 'community','email','priority','dedicated','24x7_phone'
    custom_domain BOOLEAN DEFAULT FALSE,
    features TEXT[] NOT NULL,                -- list of feature flags
    sort_order INT,
    is_public BOOLEAN DEFAULT TRUE,          -- show on /pricing page
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Workspace subscriptions
CREATE TABLE IF NOT EXISTS workspace_subscriptions (
    workspace_id VARCHAR(32) PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    plan_id VARCHAR(32) NOT NULL REFERENCES pricing_plans(id),
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('trial','active','past_due','cancelled','suspended')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_period_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_period_end TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    payment_method VARCHAR(30) DEFAULT 'manual',   -- 'manual','vietqr','vnpay','zeni_token'
    payment_reference TEXT,                          -- bank transfer ref or transaction id
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ws_sub_plan ON workspace_subscriptions(plan_id);
CREATE INDEX IF NOT EXISTS idx_ws_sub_status ON workspace_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_ws_sub_period_end ON workspace_subscriptions(current_period_end);

-- Usage counters per workspace per month
CREATE TABLE IF NOT EXISTS workspace_usage (
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    period_start DATE NOT NULL,                 -- first day of month (DATE_TRUNC('month', NOW())::DATE)
    requests_count INT NOT NULL DEFAULT 0,
    ai_tokens_count BIGINT NOT NULL DEFAULT 0,
    storage_gb_avg NUMERIC(10,3) DEFAULT 0,
    router_cost_usd NUMERIC(10,4) DEFAULT 0,
    last_request_at TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, period_start)
);
CREATE INDEX IF NOT EXISTS idx_usage_period ON workspace_usage(period_start);

-- Quota events log (for audit + alerts)
CREATE TABLE IF NOT EXISTS quota_events (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    event_type VARCHAR(40) NOT NULL,    -- 'quota_warning_80','quota_exceeded','plan_upgraded','plan_downgraded','plan_cancelled','plan_extended'
    detail TEXT,
    triggered_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quota_events_ws ON quota_events(workspace_id, triggered_at DESC);

-- ─── SEED 5 TIERS ───────────────────────────────────────────────────────────
INSERT INTO pricing_plans (
    id, name, price_vnd_monthly, price_usd_monthly,
    quota_requests_per_month, quota_ai_tokens_per_month, quota_storage_gb,
    quota_router_usd_per_month, quota_projects, quota_dev_seats,
    sla_uptime_percent, support_level, custom_domain, features, sort_order
) VALUES
  ('free',       'Free — Khám phá',         0,         0.00,
    100000,    5000000,     1,    0.50,   1,  1,
    99.0,  'community', false,
    ARRAY['ai_basic','ocr','translate','vector','sms','slack','privacy_tier1'], 1),

  ('starter',    'Starter — Khởi tạo',      999000,    40.00,
    1000000,   100000000,   10,   5.00,   5,  3,
    99.5,  'email',     true,
    ARRAY['ai_full','ocr','translate','vector','sms','slack','custom_domain','privacy_tier1'], 2),

  ('pro',        'Pro — Doanh nghiệp',      4900000,   200.00,
    10000000,  1000000000,  100,  50.00,  -1, 10,
    99.9,  'priority',  true,
    ARRAY['ai_full','ocr','translate','vector','sms','slack','custom_domain','privacy_tier2','smart_contract','vertical_models'], 3),

  ('business',   'Business — Tập đoàn',     49000000,  2000.00,
    100000000, 10000000000, 1024, 500.00, -1, 50,
    99.95, 'dedicated', true,
    ARRAY['all','dedicated_csm','sla','privacy_tier3','soc2_pending'], 4),

  ('enterprise','Enterprise — Riêng biệt', 199000000, 8000.00,
    1000000000, 100000000000, 10240, 5000.00, -1, 500,
    99.99, '24x7_phone', true,
    ARRAY['all','dedicated_infra','on_prem','iso27001','soc2','gdpr','privacy_tier4'], 5)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    price_vnd_monthly = EXCLUDED.price_vnd_monthly,
    price_usd_monthly = EXCLUDED.price_usd_monthly,
    quota_requests_per_month = EXCLUDED.quota_requests_per_month,
    quota_ai_tokens_per_month = EXCLUDED.quota_ai_tokens_per_month,
    quota_storage_gb = EXCLUDED.quota_storage_gb,
    quota_router_usd_per_month = EXCLUDED.quota_router_usd_per_month,
    quota_projects = EXCLUDED.quota_projects,
    quota_dev_seats = EXCLUDED.quota_dev_seats,
    sla_uptime_percent = EXCLUDED.sla_uptime_percent,
    support_level = EXCLUDED.support_level,
    custom_domain = EXCLUDED.custom_domain,
    features = EXCLUDED.features,
    sort_order = EXCLUDED.sort_order;

-- Default Free tier cho mọi workspace cũ chưa có subscription
INSERT INTO workspace_subscriptions (workspace_id, plan_id, status)
SELECT id, 'free', 'active' FROM workspaces
WHERE id NOT IN (SELECT workspace_id FROM workspace_subscriptions)
ON CONFLICT (workspace_id) DO NOTHING;
