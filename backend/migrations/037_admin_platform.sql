-- ============================================================================
-- Migration 037 — Platform Admin View (Sprint A4)
--
-- Purpose: Tách quyền giữa "Customer Owner" và "Platform Admin" của Zeni Cloud.
--   Platform Admin (caotuanphat581@gmail.com) là super-admin của hệ thống —
--   xem được aggregate stats của TOÀN bộ platform, KHÔNG truy cập raw data
--   của khách (phải đi qua CAAA flow Sprint A3 — Customer-Authorized
--   Admin Access).
--
-- Phân biệt với role 'Owner':
--   * Owner       : người sở hữu workspace của khách (ví dụ Owner của ws_acme)
--   * PlatformAdmin: super-admin của Zeni Cloud — quản lý nền tảng tổng thể
--
-- Tables (5):
--   1. platform_alerts            — Cloud Run errors, SLA breaches, monitoring
--   2. platform_announcements     — Banner/email cho user theo role + lịch
--   3. platform_feature_flags     — Toggle bật/tắt feature theo environment
--   4. platform_support_tickets   — Helpdesk inbox cho Customer Support
--   5. platform_admin_actions     — Audit trail riêng cho Platform Admin
--
-- An toàn:
--   * Tất cả endpoints /admin/platform/* cần require_platform_admin gate
--   * audit_push vào platform_admin_actions cho mọi sensitive action
--   * KHÔNG bao giờ trả raw data của customer; chỉ aggregate / summary
-- ============================================================================

-- ─── 1. Platform Alerts ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_alerts (
    id              BIGSERIAL PRIMARY KEY,
    alert_type      VARCHAR(64) NOT NULL,           -- 'cloud_run_error','sla_breach','quota_exceeded','db_slow','cost_spike'
    severity        VARCHAR(16) NOT NULL DEFAULT 'warn',  -- 'info','warn','error','critical'
    message         TEXT NOT NULL,
    source          VARCHAR(128),                    -- 'cloud_run/zeni-cloud-api','cloud_sql/zeni-pg-prod', etc.
    details         JSONB DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(255)                     -- email of platform admin who resolved
);
CREATE INDEX IF NOT EXISTS idx_platform_alerts_unresolved
    ON platform_alerts(occurred_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_platform_alerts_type
    ON platform_alerts(alert_type, severity, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_alerts_severity
    ON platform_alerts(severity, occurred_at DESC);


-- ─── 2. Platform Announcements ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_announcements (
    id              BIGSERIAL PRIMARY KEY,
    title           VARCHAR(255) NOT NULL,
    content         TEXT NOT NULL,                   -- markdown / html safe
    target_role     VARCHAR(64) NOT NULL DEFAULT 'all',  -- 'all','Owner','Developer','Viewer','PlatformAdmin'
    scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    is_pinned       BOOLEAN NOT NULL DEFAULT FALSE,
    severity        VARCHAR(16) NOT NULL DEFAULT 'info',  -- 'info','warn','critical'
    created_by      VARCHAR(255),                     -- platform admin email
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_platform_announce_active
    ON platform_announcements(scheduled_at DESC, expires_at);
CREATE INDEX IF NOT EXISTS idx_platform_announce_role
    ON platform_announcements(target_role, scheduled_at DESC);


-- ─── 3. Platform Feature Flags ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_feature_flags (
    id              BIGSERIAL PRIMARY KEY,
    key             VARCHAR(120) NOT NULL,
    value           JSONB NOT NULL DEFAULT 'false'::jsonb,
    description     TEXT,
    environment     VARCHAR(16) NOT NULL DEFAULT 'prod',  -- 'dev','staging','prod'
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      VARCHAR(255),
    UNIQUE (key, environment)
);
CREATE INDEX IF NOT EXISTS idx_platform_flags_env
    ON platform_feature_flags(environment, key);


-- ─── 4. Platform Support Tickets ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_support_tickets (
    id                      BIGSERIAL PRIMARY KEY,
    customer_workspace_id   VARCHAR(32) REFERENCES workspaces(id) ON DELETE SET NULL,
    customer_email          VARCHAR(255) NOT NULL,
    subject                 VARCHAR(255) NOT NULL,
    description             TEXT NOT NULL,
    status                  VARCHAR(20) NOT NULL DEFAULT 'open',     -- 'open','pending','resolved','closed'
    priority                VARCHAR(10) NOT NULL DEFAULT 'normal',   -- 'low','normal','high','urgent'
    assigned_admin          VARCHAR(255),                             -- email of platform admin
    source                  VARCHAR(32) DEFAULT 'web',                -- 'web','email','chat','api','phone'
    tags                    TEXT[] DEFAULT ARRAY[]::TEXT[],
    metadata                JSONB DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at             TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_platform_tickets_status
    ON platform_support_tickets(status, priority DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_tickets_assigned
    ON platform_support_tickets(assigned_admin, status);
CREATE INDEX IF NOT EXISTS idx_platform_tickets_workspace
    ON platform_support_tickets(customer_workspace_id, created_at DESC);


-- ─── 5. Platform Admin Actions (audit trail) ────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_admin_actions (
    id              BIGSERIAL PRIMARY KEY,
    admin_email     VARCHAR(255) NOT NULL,
    action_type     VARCHAR(80) NOT NULL,            -- 'view_dashboard','impersonate','feature_flag.update','ticket.assign','alert.resolve', etc.
    target_type     VARCHAR(40),                      -- 'workspace','ticket','alert','feature_flag','announcement','admin_request'
    target_id       VARCHAR(120),                     -- workspace_id, ticket_id, etc.
    details         JSONB DEFAULT '{}'::jsonb,
    ip_address      VARCHAR(64),
    user_agent      VARCHAR(512),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_platform_admin_actions_admin
    ON platform_admin_actions(admin_email, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_admin_actions_type
    ON platform_admin_actions(action_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_admin_actions_target
    ON platform_admin_actions(target_type, target_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_platform_admin_actions_time
    ON platform_admin_actions(occurred_at DESC);


-- ─── Seed feature flags ─────────────────────────────────────────────────────
INSERT INTO platform_feature_flags (key, value, description, environment) VALUES
    ('signup_enabled',             'true'::jsonb,  'Allow self-service signup',                        'prod'),
    ('ai_training_optin_default',  'false'::jsonb, 'Default opt-in state for new signups',             'prod'),
    ('beta_features_enabled',      'true'::jsonb,  'Enable beta features for all',                     'prod'),
    ('maintenance_mode',           'false'::jsonb, 'Show maintenance banner',                          'prod'),
    ('zeni_pay_cap_2_enabled',     'true'::jsonb,  'Wallet system enabled',                            'prod')
ON CONFLICT (key, environment) DO NOTHING;


-- ─── Helper view: customer summary (aggregate, no raw data) ─────────────────
-- Cho phép Platform Admin nhìn workspace high-level mà không động raw rows.
CREATE OR REPLACE VIEW v_platform_customer_summary AS
SELECT
    w.id                                                AS workspace_id,
    w.code                                              AS workspace_code,
    w.name                                              AS workspace_name,
    w.created_at                                        AS workspace_created_at,
    (
        SELECT u.email FROM users u
        JOIN user_workspaces uw ON uw.user_id = u.id
        WHERE uw.workspace_id = w.id AND uw.role = 'Owner'
        ORDER BY u.created_at ASC NULLS LAST LIMIT 1
    )                                                   AS owner_email,
    (
        SELECT COUNT(*) FROM user_workspaces uw
        WHERE uw.workspace_id = w.id
    )                                                   AS member_count,
    (
        SELECT MAX(b.ts) FROM billing_events b
        WHERE b.workspace_id = w.id
    )                                                   AS last_billing_event_at,
    (
        SELECT COALESCE(SUM(b.cost_usd), 0) FROM billing_events b
        WHERE b.workspace_id = w.id
          AND b.ts >= NOW() - INTERVAL '30 days'
    )                                                   AS spend_usd_30d,
    (
        SELECT COALESCE(SUM(b.cost_usd), 0) FROM billing_events b
        WHERE b.workspace_id = w.id
    )                                                   AS spend_usd_lifetime
FROM workspaces w;


-- ─── Migration log row (best-effort) ────────────────────────────────────────
DO $$
BEGIN
    -- migration_log table có thể chưa tồn tại — bọc trong DO block
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'migration_log') THEN
        INSERT INTO migration_log(version, description, applied_at)
        VALUES ('037', 'Platform Admin View — alerts/announcements/flags/tickets/actions', NOW())
        ON CONFLICT DO NOTHING;
    END IF;
END $$;
