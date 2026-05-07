-- ============================================================================
-- Migration 041 — White-label & Reseller Program (Phase 4)
--
-- Cho phép các agency / partner bán Zeni Cloud dưới brand riêng (cloud.brand.com).
-- Reseller có thể custom logo / màu sắc / domain, quản lý khách của mình,
-- nhận revenue share theo tier.
--
-- Tables (6):
--   1. reseller_accounts        — application + tier + commission %
--   2. reseller_brand_config    — logo / màu / domain / SMTP / footer
--   3. reseller_customers       — workspace của khách thuộc reseller nào
--   4. reseller_commissions     — hoa hồng theo billing period
--   5. reseller_promo_codes     — promo code reseller phát cho khách
--   6. reseller_payouts         — batch payout (chuyển tiền cho reseller)
--
-- An toàn:
--   * status = 'pending' khi apply, phải qua duyệt ('approved') mới active
--   * Tất cả endpoints /reseller/* đều scope theo workspace_id của reseller
--   * Commission được lock vào trạng thái 'payable' sau X ngày, mới chuyển 'paid'
-- ============================================================================

-- ─── 1. Reseller Accounts ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reseller_accounts (
    id                  BIGSERIAL PRIMARY KEY,
    workspace_id        VARCHAR(32) NOT NULL UNIQUE
                            REFERENCES workspaces(id) ON DELETE CASCADE,
    reseller_name       VARCHAR(120) NOT NULL,
    business_name       VARCHAR(255),
    contact_email       VARCHAR(255) NOT NULL,
    contact_phone       VARCHAR(64),
    tax_id              VARCHAR(64),
    tier                VARCHAR(16) NOT NULL DEFAULT 'basic',  -- 'basic','pro','elite'
    commission_percent  NUMERIC(5,2) NOT NULL DEFAULT 15.00,   -- % cut on customer paid
    discount_percent    NUMERIC(5,2) NOT NULL DEFAULT 0.00,    -- % off list price
    payout_method       VARCHAR(32) DEFAULT 'bank_transfer',   -- 'bank_transfer','vnpay','crypto'
    payout_account      VARCHAR(255),                          -- account number / wallet
    status              VARCHAR(16) NOT NULL DEFAULT 'pending',-- 'pending','approved','suspended','rejected'
    approved_at         TIMESTAMPTZ,
    approved_by         VARCHAR(255),                          -- platform admin email
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reseller_accounts_status
    ON reseller_accounts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reseller_accounts_tier
    ON reseller_accounts(tier, status);


-- ─── 2. Reseller Brand Config ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reseller_brand_config (
    reseller_id         BIGINT PRIMARY KEY
                            REFERENCES reseller_accounts(id) ON DELETE CASCADE,
    brand_name          VARCHAR(120),
    logo_url            VARCHAR(500),
    favicon_url         VARCHAR(500),
    primary_color       VARCHAR(16)  DEFAULT '#6366F1',
    secondary_color     VARCHAR(16)  DEFAULT '#A855F7',
    accent_color        VARCHAR(16)  DEFAULT '#22D3EE',
    custom_domain       VARCHAR(255) UNIQUE,                   -- 'cloud.brand.com'
    custom_email_from   VARCHAR(255),                          -- 'noreply@brand.com'
    custom_smtp_config  JSONB DEFAULT '{}'::jsonb,             -- { host, port, user, pass_enc, ... }
    support_email       VARCHAR(255),
    terms_url           VARCHAR(500),
    privacy_url         VARCHAR(500),
    footer_html         TEXT,
    custom_css          TEXT,
    domain_verified_at  TIMESTAMPTZ,
    domain_cname_token  VARCHAR(64),                           -- random token for CNAME challenge
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reseller_brand_domain
    ON reseller_brand_config(custom_domain) WHERE custom_domain IS NOT NULL;


-- ─── 3. Reseller Customers ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reseller_customers (
    id                      BIGSERIAL PRIMARY KEY,
    reseller_id             BIGINT NOT NULL
                                REFERENCES reseller_accounts(id) ON DELETE CASCADE,
    customer_workspace_id   VARCHAR(32) NOT NULL UNIQUE
                                REFERENCES workspaces(id) ON DELETE CASCADE,
    customer_email          VARCHAR(255) NOT NULL,
    signed_up_via           VARCHAR(64) DEFAULT 'invite',      -- 'invite','promo_code','custom_domain','referral'
    promo_code              VARCHAR(40),
    original_plan           VARCHAR(32) DEFAULT 'free',
    current_plan            VARCHAR(32) DEFAULT 'free',
    status                  VARCHAR(16) NOT NULL DEFAULT 'active', -- 'active','churned','suspended'
    lifetime_value_vnd      NUMERIC(18,2) NOT NULL DEFAULT 0,
    last_payment_at         TIMESTAMPTZ,
    churned_at              TIMESTAMPTZ,
    signed_up_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reseller_customers_reseller
    ON reseller_customers(reseller_id, status, signed_up_at DESC);
CREATE INDEX IF NOT EXISTS idx_reseller_customers_workspace
    ON reseller_customers(customer_workspace_id);
CREATE INDEX IF NOT EXISTS idx_reseller_customers_email
    ON reseller_customers(customer_email);


-- ─── 4. Reseller Commissions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reseller_commissions (
    id                      BIGSERIAL PRIMARY KEY,
    reseller_id             BIGINT NOT NULL
                                REFERENCES reseller_accounts(id) ON DELETE CASCADE,
    customer_workspace_id   VARCHAR(32) NOT NULL,
    billing_period_start    TIMESTAMPTZ NOT NULL,
    billing_period_end      TIMESTAMPTZ NOT NULL,
    customer_paid_vnd       NUMERIC(18,2) NOT NULL DEFAULT 0,
    commission_percent      NUMERIC(5,2) NOT NULL DEFAULT 15.00,
    commission_vnd          NUMERIC(18,2) NOT NULL DEFAULT 0,
    status                  VARCHAR(16) NOT NULL DEFAULT 'pending',
        -- 'pending'  : just computed
        -- 'payable'  : eligible (after grace period)
        -- 'paid'     : transferred to reseller payout account
        -- 'clawback' : refunded → commission reversed
    payable_at              TIMESTAMPTZ,
    paid_at                 TIMESTAMPTZ,
    payout_id               BIGINT,                            -- → reseller_payouts.id (FK soft, set later)
    payment_reference       VARCHAR(128),
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (reseller_id, customer_workspace_id, billing_period_start)
);
CREATE INDEX IF NOT EXISTS idx_reseller_comm_reseller
    ON reseller_commissions(reseller_id, status, billing_period_end DESC);
CREATE INDEX IF NOT EXISTS idx_reseller_comm_status
    ON reseller_commissions(status, payable_at);
CREATE INDEX IF NOT EXISTS idx_reseller_comm_period
    ON reseller_commissions(billing_period_start, billing_period_end);


-- ─── 5. Reseller Promo Codes ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reseller_promo_codes (
    id                  BIGSERIAL PRIMARY KEY,
    reseller_id         BIGINT NOT NULL
                            REFERENCES reseller_accounts(id) ON DELETE CASCADE,
    code                VARCHAR(40) NOT NULL UNIQUE,
    discount_type       VARCHAR(16) NOT NULL DEFAULT 'percent', -- 'percent','fixed'
    discount_value      NUMERIC(10,2) NOT NULL DEFAULT 0,
    max_uses            INTEGER,                                -- NULL = unlimited
    current_uses        INTEGER NOT NULL DEFAULT 0,
    expires_at          TIMESTAMPTZ,
    applies_to_plans    VARCHAR(40)[] DEFAULT ARRAY[]::VARCHAR(40)[],
    description         TEXT,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reseller_promo_reseller
    ON reseller_promo_codes(reseller_id, enabled);
CREATE INDEX IF NOT EXISTS idx_reseller_promo_code
    ON reseller_promo_codes(code) WHERE enabled = TRUE;


-- ─── 6. Reseller Payouts ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reseller_payouts (
    id                  BIGSERIAL PRIMARY KEY,
    reseller_id         BIGINT NOT NULL
                            REFERENCES reseller_accounts(id) ON DELETE CASCADE,
    total_amount_vnd    NUMERIC(18,2) NOT NULL DEFAULT 0,
    period_start        TIMESTAMPTZ NOT NULL,
    period_end          TIMESTAMPTZ NOT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'pending', -- 'pending','processing','paid','failed'
    paid_at             TIMESTAMPTZ,
    payment_method      VARCHAR(32),
    payout_account      VARCHAR(255),
    transaction_ref     VARCHAR(255),
    error_message       TEXT,
    commission_count    INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reseller_payouts_reseller
    ON reseller_payouts(reseller_id, status, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_reseller_payouts_status
    ON reseller_payouts(status, created_at DESC);


-- ─── Helper view: reseller dashboard summary ────────────────────────────────
CREATE OR REPLACE VIEW v_reseller_dashboard AS
SELECT
    ra.id                                                       AS reseller_id,
    ra.workspace_id                                             AS reseller_workspace_id,
    ra.reseller_name,
    ra.tier,
    ra.commission_percent,
    ra.status                                                   AS reseller_status,
    (SELECT COUNT(*) FROM reseller_customers rc
        WHERE rc.reseller_id = ra.id AND rc.status = 'active')  AS active_customers,
    (SELECT COUNT(*) FROM reseller_customers rc
        WHERE rc.reseller_id = ra.id AND rc.status = 'churned') AS churned_customers,
    (SELECT COALESCE(SUM(rc.lifetime_value_vnd), 0)
        FROM reseller_customers rc
        WHERE rc.reseller_id = ra.id)                            AS total_customer_value_vnd,
    (SELECT COALESCE(SUM(rcc.commission_vnd), 0)
        FROM reseller_commissions rcc
        WHERE rcc.reseller_id = ra.id AND rcc.status = 'paid')   AS commission_paid_vnd,
    (SELECT COALESCE(SUM(rcc.commission_vnd), 0)
        FROM reseller_commissions rcc
        WHERE rcc.reseller_id = ra.id AND rcc.status IN ('pending','payable')) AS commission_pending_vnd,
    (SELECT COALESCE(SUM(rcc.commission_vnd), 0)
        FROM reseller_commissions rcc
        WHERE rcc.reseller_id = ra.id
          AND rcc.billing_period_end >= NOW() - INTERVAL '30 days')  AS commission_last_30d_vnd
FROM reseller_accounts ra;


-- ─── Migration log row (best-effort) ────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'migration_log') THEN
        INSERT INTO migration_log(version, description, applied_at)
        VALUES ('041', 'White-label reseller program — accounts/brand/customers/commissions/promo/payouts', NOW())
        ON CONFLICT DO NOTHING;
    END IF;
END $$;
