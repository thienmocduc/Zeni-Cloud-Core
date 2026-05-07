-- ============================================================================
-- Migration 024 — Zeni Pay Cấp 1 (VietQR direct payment)
--
-- Purpose: Khách subscribe → scan VietQR → chuyển khoản tới TK Zeni Holdings →
--   Zeni listen webhook ngân hàng → match intent_code → activate subscription.
--   KHÔNG qua VNPay/MoMo. Direct bank-to-bank.
--
-- Tables:
--   payment_bank_accounts  — Bank accounts của Zeni Holdings (multi-bank)
--   payment_intents        — Mỗi lần khách click "Pay" → 1 intent (TTL 30 phút)
--   bank_webhook_events    — Raw inbound từ ngân hàng (TPB/MB/VCB Open API)
--   payment_refunds        — Refund history
-- ============================================================================

-- Bank accounts của Zeni Holdings (multi-bank cho redundancy)
CREATE TABLE IF NOT EXISTS payment_bank_accounts (
    id BIGSERIAL PRIMARY KEY,
    bank_code VARCHAR(20) NOT NULL,            -- 'TPB','MB','VCB','VPB'
    bank_name VARCHAR(80) NOT NULL,
    account_number VARCHAR(40) NOT NULL,
    account_holder VARCHAR(120) NOT NULL,
    branch VARCHAR(120),
    is_active BOOLEAN DEFAULT TRUE,
    is_default BOOLEAN DEFAULT FALSE,
    webhook_secret TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (bank_code, account_number)
);
CREATE INDEX IF NOT EXISTS idx_bank_accounts_default ON payment_bank_accounts(is_default, is_active);

-- Payment intents (mỗi lần khách click "Pay" → 1 intent)
CREATE TABLE IF NOT EXISTS payment_intents (
    id BIGSERIAL PRIMARY KEY,
    intent_code VARCHAR(40) UNIQUE NOT NULL,   -- ZP-{ws}-{timestamp:base36} — match khi webhook đến
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_email TEXT NOT NULL,
    amount_vnd INT NOT NULL,
    purpose VARCHAR(40) NOT NULL,              -- 'subscription_pro','subscription_business','wallet_topup','custom'
    purpose_ref VARCHAR(80),                   -- e.g. 'plan_id=pro' or 'topup_amount=10000000'
    bank_account_id BIGINT REFERENCES payment_bank_accounts(id),
    qr_image_data TEXT,                        -- base64 PNG of generated QR
    qr_payload TEXT,                           -- TCVN VietQR string content
    status VARCHAR(20) DEFAULT 'pending',      -- 'pending','paid','expired','cancelled','refunded'
    expires_at TIMESTAMPTZ NOT NULL,           -- 30 phút TTL
    paid_at TIMESTAMPTZ,
    paid_amount_vnd INT,
    bank_tx_ref VARCHAR(80),                   -- ngân hàng tx reference
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payment_intents_ws ON payment_intents(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payment_intents_status ON payment_intents(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_payment_intents_code ON payment_intents(intent_code);

-- Bank webhook events (raw inbound data từ ngân hàng)
CREATE TABLE IF NOT EXISTS bank_webhook_events (
    id BIGSERIAL PRIMARY KEY,
    bank_code VARCHAR(20) NOT NULL,
    bank_account_id BIGINT REFERENCES payment_bank_accounts(id),
    raw_payload JSONB NOT NULL,
    parsed_amount_vnd INT,
    parsed_ref_code VARCHAR(80),
    parsed_tx_ref VARCHAR(80),
    parsed_sender_name VARCHAR(120),
    matched_intent_id BIGINT REFERENCES payment_intents(id),
    processed BOOLEAN DEFAULT FALSE,
    processing_error TEXT,
    received_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bank_webhook_unprocessed ON bank_webhook_events(processed, received_at) WHERE NOT processed;
CREATE INDEX IF NOT EXISTS idx_bank_webhook_ref ON bank_webhook_events(parsed_ref_code);

-- Refund history
CREATE TABLE IF NOT EXISTS payment_refunds (
    id BIGSERIAL PRIMARY KEY,
    intent_id BIGINT NOT NULL REFERENCES payment_intents(id),
    workspace_id VARCHAR(32) NOT NULL,
    amount_vnd INT NOT NULL,
    reason TEXT,
    refunded_by_user TEXT,                     -- admin email
    status VARCHAR(20) DEFAULT 'pending',
    refunded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payment_refunds_ws ON payment_refunds(workspace_id, created_at DESC);

-- Seed default Zeni Holdings bank account (placeholder — anh CEO update later)
INSERT INTO payment_bank_accounts (bank_code, bank_name, account_number, account_holder, branch, is_default, is_active)
VALUES ('TPB', 'TPBank', '00000000000', 'CONG TY ZENI HOLDINGS', 'Hoi so', true, true)
ON CONFLICT (bank_code, account_number) DO NOTHING;
