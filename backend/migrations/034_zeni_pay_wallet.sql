-- ============================================================================
-- Migration 034 — Zeni Pay Cấp 2 (Internal Wallet)
--
-- Purpose: Mở rộng Zeni Pay Cấp 1 (VietQR direct payment ở migration 024)
--   thành internal wallet system. User nạp tiền 1 lần qua VietQR → balance
--   được giữ trong wallet → mọi giao dịch Zeni Cloud (subscription, router,
--   agent run, transfer) deduct trực tiếp từ wallet balance.
--
-- Design notes:
--   - wallet_balances (đã có từ migration 011) → ALTER thêm cột (balance_locked,
--     escrow_amount, currency, last_charged_at đã có).
--   - wallet_transactions (đã có từ 011) → ALTER thêm cột type detail
--     (type[topup/spend/refund/transfer_in/transfer_out/lock/unlock/escrow/release],
--     source_type, source_id, status). Cột "kind" cũ vẫn giữ để backward-compat.
--   - wallet_topups (mới) — link giữa payment_intents và wallet (Cấp 2 specific).
--   - wallet_holds (mới) — temporary escrow cho AI agent run / blockchain tx.
--   - wallet_recurring_charges (mới) — auto-deduct subscription mỗi 30 ngày.
--   - wallet_alerts (mới) — low balance / charge failed / refund alerts.
--
-- Idempotent: dùng IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / ON CONFLICT.
-- ============================================================================

-- ─── 1. ALTER wallet_balances (đã tồn tại từ migration 011) ─────────────────
ALTER TABLE wallet_balances
    ADD COLUMN IF NOT EXISTS balance_locked NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS escrow_amount  NUMERIC(14,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS currency       VARCHAR(8)    NOT NULL DEFAULT 'VND',
    ADD COLUMN IF NOT EXISTS low_balance_threshold NUMERIC(14,2) NOT NULL DEFAULT 50000;

-- Useful index for low-balance scans
CREATE INDEX IF NOT EXISTS idx_wallet_balances_low ON wallet_balances(balance_vnd)
    WHERE balance_vnd < 100000;

-- ─── 2. ALTER wallet_transactions (đã tồn tại từ migration 011) ─────────────
-- Cột "kind" cũ (topup|charge|refund|sub_payment) vẫn giữ.
-- Thêm cột mới cho granularity rộng hơn của Cấp 2.
ALTER TABLE wallet_transactions
    ADD COLUMN IF NOT EXISTS type        VARCHAR(20),
    ADD COLUMN IF NOT EXISTS source_type VARCHAR(40),
    ADD COLUMN IF NOT EXISTS source_id   VARCHAR(80),
    ADD COLUMN IF NOT EXISTS status      VARCHAR(20) NOT NULL DEFAULT 'completed',
    ADD COLUMN IF NOT EXISTS related_tx_id BIGINT;

-- Backfill type from kind for existing rows (one-time)
UPDATE wallet_transactions
   SET type = CASE
        WHEN kind = 'topup' THEN 'topup'
        WHEN kind = 'charge' THEN 'spend'
        WHEN kind = 'refund' THEN 'refund'
        WHEN kind = 'sub_payment' THEN 'spend'
        ELSE kind
       END
 WHERE type IS NULL;

-- Add CHECK constraint for type (drop first if exists)
ALTER TABLE wallet_transactions DROP CONSTRAINT IF EXISTS wallet_tx_type_chk;
ALTER TABLE wallet_transactions ADD CONSTRAINT wallet_tx_type_chk CHECK (
    type IN ('topup','spend','refund','transfer_in','transfer_out',
             'lock','unlock','escrow','release','adjust')
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_source ON wallet_transactions(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_type   ON wallet_transactions(workspace_id, type, created_at DESC);

-- ─── 3. wallet_topups — link giữa payment_intents và wallet ─────────────────
CREATE TABLE IF NOT EXISTS wallet_topups (
    id            BIGSERIAL    PRIMARY KEY,
    workspace_id  VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    intent_id     BIGINT REFERENCES payment_intents(id) ON DELETE SET NULL,
    intent_code   VARCHAR(40),                    -- denormalized for lookup
    amount_vnd    NUMERIC(14,2) NOT NULL,
    payment_method VARCHAR(40)  NOT NULL DEFAULT 'vietqr',
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending|completed|failed|expired
    completed_at  TIMESTAMPTZ,
    metadata      JSONB        DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wallet_topups_ws     ON wallet_topups(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_topups_status ON wallet_topups(status, created_at);
CREATE INDEX IF NOT EXISTS idx_wallet_topups_intent ON wallet_topups(intent_code);

-- ─── 4. wallet_holds — temporary holds (escrow) ─────────────────────────────
-- Cho phép lock 1 phần balance khi user submit AI agent run mà chưa biết
-- chính xác chi phí. Sau khi agent xong → release hold + spend amount thực tế.
CREATE TABLE IF NOT EXISTS wallet_holds (
    id            BIGSERIAL    PRIMARY KEY,
    workspace_id  VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    amount_vnd    NUMERIC(14,2) NOT NULL,
    reason        VARCHAR(120) NOT NULL,
    source_type   VARCHAR(40),                    -- 'agent_run'|'router_call'|'blockchain_tx'
    source_id     VARCHAR(80),
    hold_until    TIMESTAMPTZ  NOT NULL,
    released      BOOLEAN      NOT NULL DEFAULT FALSE,
    released_at   TIMESTAMPTZ,
    actual_spent  NUMERIC(14,2),                  -- amount actually consumed at release
    metadata      JSONB        DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wallet_holds_ws     ON wallet_holds(workspace_id, released, hold_until);
CREATE INDEX IF NOT EXISTS idx_wallet_holds_active ON wallet_holds(hold_until) WHERE NOT released;

-- ─── 5. wallet_recurring_charges — subscription auto-deduct ─────────────────
CREATE TABLE IF NOT EXISTS wallet_recurring_charges (
    id            BIGSERIAL    PRIMARY KEY,
    workspace_id  VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    plan_id       VARCHAR(40)  NOT NULL,
    amount_vnd    NUMERIC(14,2) NOT NULL,
    billing_cycle VARCHAR(16)  NOT NULL DEFAULT 'monthly',  -- monthly|yearly
    next_charge_at TIMESTAMPTZ NOT NULL,
    last_charged_at TIMESTAMPTZ,
    last_charge_status VARCHAR(20),               -- 'success'|'failed_insufficient'|'failed_other'
    retry_count   INT          NOT NULL DEFAULT 0,
    max_retries   INT          NOT NULL DEFAULT 3,
    status        VARCHAR(20)  NOT NULL DEFAULT 'active', -- active|paused|cancelled
    cancelled_at  TIMESTAMPTZ,
    metadata      JSONB        DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wallet_recur_due
    ON wallet_recurring_charges(next_charge_at, status)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_wallet_recur_ws
    ON wallet_recurring_charges(workspace_id, status);

-- ─── 6. wallet_alerts — low balance / charge failed / refund notifications ──
CREATE TABLE IF NOT EXISTS wallet_alerts (
    id            BIGSERIAL    PRIMARY KEY,
    workspace_id  VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    alert_type    VARCHAR(30)  NOT NULL,  -- 'low_balance'|'charge_failed'|'refund'|'topup_received'
    threshold_vnd NUMERIC(14,2),          -- only for low_balance
    email_enabled BOOLEAN      NOT NULL DEFAULT TRUE,
    sms_enabled   BOOLEAN      NOT NULL DEFAULT FALSE,
    last_triggered_at TIMESTAMPTZ,
    trigger_count INT          NOT NULL DEFAULT 0,
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    metadata      JSONB        DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, alert_type)
);
CREATE INDEX IF NOT EXISTS idx_wallet_alerts_ws ON wallet_alerts(workspace_id, enabled);

-- ─── 7. Trigger: keep wallet_balances.updated_at fresh on UPDATE ────────────
CREATE OR REPLACE FUNCTION wallet_balances_touch_updated()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_wallet_balances_touch ON wallet_balances;
CREATE TRIGGER trg_wallet_balances_touch
    BEFORE UPDATE ON wallet_balances
    FOR EACH ROW EXECUTE FUNCTION wallet_balances_touch_updated();

-- ─── 8. Default low-balance alert for existing workspaces ───────────────────
INSERT INTO wallet_alerts (workspace_id, alert_type, threshold_vnd, email_enabled)
    SELECT id, 'low_balance', 50000, TRUE FROM workspaces
ON CONFLICT (workspace_id, alert_type) DO NOTHING;

-- ─── 9. GRANTS ──────────────────────────────────────────────────────────────
GRANT ALL PRIVILEGES ON wallet_topups            TO zeni_app;
GRANT ALL PRIVILEGES ON wallet_holds             TO zeni_app;
GRANT ALL PRIVILEGES ON wallet_recurring_charges TO zeni_app;
GRANT ALL PRIVILEGES ON wallet_alerts            TO zeni_app;
GRANT USAGE, SELECT ON wallet_topups_id_seq            TO zeni_app;
GRANT USAGE, SELECT ON wallet_holds_id_seq             TO zeni_app;
GRANT USAGE, SELECT ON wallet_recurring_charges_id_seq TO zeni_app;
GRANT USAGE, SELECT ON wallet_alerts_id_seq            TO zeni_app;
