-- ═══════════════════════════════════════════════════════════
-- 018: Privacy Preferences + Admin Access Requests + Output Filter Logs
--
-- Mục tiêu:
--   1. Cho phép từng workspace cấu hình privacy (opt-in AI training,
--      data region, CMEK, DPA / Terms version đã ký)
--   2. Track admin access requests (link sang Zeni Chain smart contract
--      khi launch on-chain governance)
--   3. Log mọi lần Output Filter chặn agent leak PII / cross-tenant
--
-- Nguyên tắc:
--   - workspaces.id là VARCHAR(32), không phải BIGINT
--   - users.id là UUID, không phải BIGINT
--   - Idempotent: CREATE IF NOT EXISTS, INSERT ON CONFLICT
--   - Default privacy = opt-out (an toàn cho user)
-- ═══════════════════════════════════════════════════════════

-- ── 1. Privacy preferences per workspace ───────────────────
CREATE TABLE IF NOT EXISTS privacy_preferences (
    workspace_id            VARCHAR(32)  PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    ai_training_opt_in      BOOLEAN      NOT NULL DEFAULT FALSE,
    ai_training_opted_in_at TIMESTAMPTZ,
    ai_training_opted_out_at TIMESTAMPTZ,
    data_region             VARCHAR(20)  NOT NULL DEFAULT 'us-central1',
    cmek_key_name           VARCHAR(255),
    cmek_enabled_at         TIMESTAMPTZ,
    terms_accepted_at       TIMESTAMPTZ,
    terms_version           VARCHAR(20),
    dpa_signed_at           TIMESTAMPTZ,
    dpa_version             VARCHAR(20),
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── 2. Admin access requests (link sang on-chain smart contract) ──
CREATE TABLE IF NOT EXISTS admin_access_requests (
    id                      BIGSERIAL    PRIMARY KEY,
    onchain_request_id      BIGINT,
    onchain_tx_hash         VARCHAR(80),
    admin_user_id           UUID         REFERENCES users(id) ON DELETE SET NULL,
    customer_workspace_id   VARCHAR(32)  REFERENCES workspaces(id) ON DELETE CASCADE,
    scope                   VARCHAR(255),
    reason                  VARCHAR(50)  NOT NULL CHECK (reason IN ('customer_support', 'legal_authority')),
    reason_detail           TEXT,
    duration_seconds        INTEGER      NOT NULL CHECK (duration_seconds BETWEEN 21600 AND 86400),
    status                  VARCHAR(20)  NOT NULL DEFAULT 'pending'
                                         CHECK (status IN ('pending', 'approved', 'revoked', 'expired')),
    requested_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    approved_at             TIMESTAMPTZ,
    expires_at              TIMESTAMPTZ,
    revoked_at              TIMESTAMPTZ,
    court_order_hash        VARCHAR(80),
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_access_workspace
    ON admin_access_requests(customer_workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_admin_access_admin
    ON admin_access_requests(admin_user_id, requested_at DESC);

-- ── 3. Output filter logs (khi agent cố leak) ──────────────
CREATE TABLE IF NOT EXISTS output_filter_logs (
    id              BIGSERIAL    PRIMARY KEY,
    workspace_id    VARCHAR(32)  REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         UUID         REFERENCES users(id) ON DELETE SET NULL,
    agent_name      VARCHAR(100),
    leak_type       VARCHAR(50)  NOT NULL,
    blocked_excerpt TEXT,
    severity        VARCHAR(20)  NOT NULL DEFAULT 'warning',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_output_filter_workspace
    ON output_filter_logs(workspace_id, created_at DESC);

-- ── 4. Default opt-out cho mọi workspace đã tồn tại ────────
INSERT INTO privacy_preferences (workspace_id)
SELECT id FROM workspaces
ON CONFLICT (workspace_id) DO NOTHING;
