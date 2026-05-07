-- Migration 054 — Outgoing Payouts (P2#11 ClawWits)
-- Unified outgoing payment: bank | zeni_token ($ZENI on Polygon) | usdt | stripe (opt-in)

CREATE TABLE IF NOT EXISTS payouts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id UUID,
  -- Recipient
  recipient_id VARCHAR(120),                  -- internal user_id or maker_id
  recipient_name VARCHAR(200),
  recipient_email VARCHAR(200),
  -- Method + amount
  method VARCHAR(40) NOT NULL,                -- bank | zeni_token | usdt | stripe | paypal
  amount_vnd BIGINT,                          -- for bank/stripe/paypal
  amount_zeni NUMERIC(30,8),                  -- for zeni_token
  amount_usdt NUMERIC(30,8),                  -- for usdt
  exchange_rate_vnd_zeni NUMERIC(20,8),       -- snapshot rate at time of payout
  -- Method-specific details
  bank_code VARCHAR(20),                      -- VCB | TPB | MBB | BIDV | etc.
  bank_account_number VARCHAR(40),
  bank_account_name VARCHAR(200),
  recipient_wallet_address VARCHAR(80),       -- 0x... for crypto
  stripe_account_id VARCHAR(60),
  -- Tracking
  status VARCHAR(20) DEFAULT 'pending',       -- pending | approved | processing | success | failed | cancelled
  purpose VARCHAR(60),                        -- maker_commission | refund | salary | affiliate
  reference VARCHAR(120),                     -- WL-2026-001
  notes TEXT,
  -- Internal tx tracking
  zeni_token_tx_hash VARCHAR(80),             -- on-chain tx hash if method=zeni_token
  bank_provider_ref TEXT,                     -- bank's reference number if method=bank
  -- Approvals (for >threshold amounts)
  requires_approval BOOLEAN DEFAULT FALSE,
  approved_by UUID,
  approved_at TIMESTAMPTZ,
  -- Audit
  error_message TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  processed_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  CONSTRAINT chk_payout_amount CHECK (
    (method = 'zeni_token' AND amount_zeni IS NOT NULL) OR
    (method = 'usdt' AND amount_usdt IS NOT NULL) OR
    (method IN ('bank','stripe','paypal') AND amount_vnd IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_payouts_ws ON payouts(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payouts_status ON payouts(status) WHERE status IN ('pending','approved','processing');
CREATE INDEX IF NOT EXISTS idx_payouts_recipient ON payouts(workspace_id, recipient_id);

-- Per-workspace payout settings
CREATE TABLE IF NOT EXISTS payout_settings (
  workspace_id VARCHAR(64) PRIMARY KEY,
  auto_approval_threshold_vnd BIGINT DEFAULT 10000000,  -- 10M VND, above = require manual approval
  enabled_methods JSONB DEFAULT '["bank","zeni_token"]'::jsonb,
  daily_limit_vnd BIGINT DEFAULT 100000000,             -- 100M VND/day
  monthly_limit_vnd BIGINT DEFAULT 1000000000,          -- 1B VND/month
  default_bank_code VARCHAR(20),
  notify_email VARCHAR(200),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE payouts IS 'Outgoing payments: bank/zeni_token/usdt — wraps zeni_token.transfer + bank API';
COMMENT ON TABLE payout_settings IS 'Per-workspace payout limits + auto-approval thresholds';
