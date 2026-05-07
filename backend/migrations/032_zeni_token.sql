-- ============================================================================
-- Migration 032 — $ZENI Token Integration (Sprint A4)
--
-- Purpose: Tích hợp $ZENI Token (đã deploy Polygon Mainnet) vào Zeni Cloud.
--   * User pay subscription/services bằng $ZENI Token (giảm 10-15% giá)
--   * User nhận reward $ZENI cho hoạt động (signup, referral, opt-in AI training)
--   * On-chain audit log balance + transactions
--   * Soulbound badge (ZeniBadge) cho early adopter / loyal customer
--
-- Pre-deployed contracts (Polygon Mainnet):
--   $ZENI Token        : 0x2d0Ec889F3889F0a364b82039db9F8Bef78f5EC1
--   AffiliateCommission: 0x1d5963FcCfC548275293e51f0F6C7aC482E0b714
--   ZeniBadge SBT      : 0xB157c83beEeA7c7ebDB2CEa305135e3deCAeD79D
--
-- Tables:
--   token_wallets         — Wallet linked tới mỗi workspace (1-1)
--   token_transactions    — Lịch sử giao dịch (earn / spend / transfer / burn)
--   token_reward_rules    — Cấu hình reward (signup, referral, ...)
--   token_badges          — Soulbound badges đã mint (ZeniBadge SBT records)
--   token_burn_records    — Voluntary burn history (governance / commitment)
-- ============================================================================

-- ─── 1. Token wallets (1 wallet per workspace) ──────────────────────────────
CREATE TABLE IF NOT EXISTS token_wallets (
    workspace_id     VARCHAR(32) PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    eth_address      VARCHAR(42) NOT NULL,             -- Checksummed EIP-55
    chain            VARCHAR(20) DEFAULT 'polygon',
    balance_zeni     NUMERIC(38,8) DEFAULT 0,          -- Cached on-chain balance
    total_earned     NUMERIC(38,8) DEFAULT 0,          -- Lifetime earned (rewards)
    total_spent      NUMERIC(38,8) DEFAULT 0,          -- Lifetime spent (payments)
    total_burned     NUMERIC(38,8) DEFAULT 0,          -- Lifetime burned
    last_synced_at   TIMESTAMPTZ,                      -- Last RPC balance refresh
    signature        TEXT,                              -- EIP-191 signature proving ownership
    signature_nonce  TEXT,                              -- Random nonce that was signed
    linked_at        TIMESTAMPTZ DEFAULT NOW(),
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (eth_address)
);
CREATE INDEX IF NOT EXISTS idx_token_wallets_addr ON token_wallets(eth_address);
CREATE INDEX IF NOT EXISTS idx_token_wallets_synced ON token_wallets(last_synced_at);

-- ─── 2. Token transactions ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS token_transactions (
    id               BIGSERIAL PRIMARY KEY,
    workspace_id     VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    wallet_address   VARCHAR(42),                       -- denormalized for explorer search
    type             VARCHAR(20) NOT NULL,              -- 'earn','spend','transfer','burn','mint','reward'
    direction        VARCHAR(10) NOT NULL,              -- 'in','out'
    amount_zeni      NUMERIC(38,8) NOT NULL,
    vnd_value        NUMERIC(20,2),                     -- Equivalent VND at time of tx
    counterparty_ws  VARCHAR(32),                       -- Other workspace if internal transfer
    counterparty_addr VARCHAR(42),                      -- Other on-chain address
    reason           VARCHAR(80),                       -- 'signup_reward','subscription_pay','manual_transfer'
    intent_code      VARCHAR(40),                       -- Link to payment_intents if pay-with-token
    tx_hash          VARCHAR(80),                       -- On-chain hash if settled on chain
    block_number     BIGINT,
    status           VARCHAR(20) DEFAULT 'confirmed',   -- 'pending','confirmed','failed','reverted'
    settlement       VARCHAR(20) DEFAULT 'offchain',    -- 'offchain','onchain','batched'
    metadata         JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_token_tx_ws ON token_transactions(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_tx_type ON token_transactions(workspace_id, type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_tx_hash ON token_transactions(tx_hash) WHERE tx_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_token_tx_status ON token_transactions(status, settlement) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_token_tx_intent ON token_transactions(intent_code) WHERE intent_code IS NOT NULL;

-- ─── 3. Reward rules (configurable) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS token_reward_rules (
    id               BIGSERIAL PRIMARY KEY,
    action           VARCHAR(40) UNIQUE NOT NULL,        -- 'signup','referral','first_payment','ai_optin','monthly_loyalty'
    amount_zeni      NUMERIC(38,8) NOT NULL,
    max_per_user     INT DEFAULT 1,                      -- 0 = unlimited; else cap
    cooldown_seconds INT DEFAULT 0,                      -- min interval between two claims
    description      TEXT,
    active           BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_token_reward_rules_active ON token_reward_rules(active);

-- ─── 4. Reward claims ledger (track user's reward usage) ────────────────────
CREATE TABLE IF NOT EXISTS token_reward_claims (
    id               BIGSERIAL PRIMARY KEY,
    workspace_id     VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    rule_id          BIGINT NOT NULL REFERENCES token_reward_rules(id),
    action           VARCHAR(40) NOT NULL,
    amount_zeni      NUMERIC(38,8) NOT NULL,
    transaction_id   BIGINT REFERENCES token_transactions(id),
    metadata         JSONB,
    claimed_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_token_reward_claims_ws ON token_reward_claims(workspace_id, action);
CREATE INDEX IF NOT EXISTS idx_token_reward_claims_recent ON token_reward_claims(workspace_id, claimed_at DESC);

-- ─── 5. Soulbound badges (ZeniBadge SBT records) ────────────────────────────
CREATE TABLE IF NOT EXISTS token_badges (
    id               BIGSERIAL PRIMARY KEY,
    workspace_id     VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    badge_type       VARCHAR(40) NOT NULL,              -- 'early_adopter','loyal_customer','top_referrer','founder'
    eth_address      VARCHAR(42) NOT NULL,              -- Recipient wallet
    token_id         BIGINT,                            -- On-chain token id (after mint settles)
    tx_hash          VARCHAR(80),                       -- Mint transaction hash
    metadata_uri     TEXT,                              -- Optional IPFS / HTTPS json
    status           VARCHAR(20) DEFAULT 'pending',     -- 'pending','minted','failed'
    minted_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, badge_type)
);
CREATE INDEX IF NOT EXISTS idx_token_badges_ws ON token_badges(workspace_id);
CREATE INDEX IF NOT EXISTS idx_token_badges_type ON token_badges(badge_type, status);

-- ─── 6. Burn records (voluntary burn for governance / commitment) ───────────
CREATE TABLE IF NOT EXISTS token_burn_records (
    id               BIGSERIAL PRIMARY KEY,
    workspace_id     VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    amount_zeni      NUMERIC(38,8) NOT NULL,
    reason           VARCHAR(120) NOT NULL,             -- 'governance_vote','commitment','milestone'
    tx_hash          VARCHAR(80),
    block_number     BIGINT,
    status           VARCHAR(20) DEFAULT 'pending',     -- 'pending','confirmed','failed'
    metadata         JSONB,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_token_burn_ws ON token_burn_records(workspace_id, created_at DESC);

-- ─── 7. Exchange rate snapshots (ZENI/VND price oracle history) ─────────────
CREATE TABLE IF NOT EXISTS token_exchange_rates (
    id               BIGSERIAL PRIMARY KEY,
    base_currency    VARCHAR(10) DEFAULT 'ZENI',
    quote_currency   VARCHAR(10) DEFAULT 'VND',
    rate             NUMERIC(20,8) NOT NULL,            -- 1 ZENI = ? VND
    rate_usd         NUMERIC(20,8),                     -- 1 ZENI = ? USD
    source           VARCHAR(40) DEFAULT 'oracle',      -- 'oracle','manual','dex'
    captured_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_token_rate_recent ON token_exchange_rates(captured_at DESC);

-- ─── Seed: Reward rules ─────────────────────────────────────────────────────
INSERT INTO token_reward_rules (action, amount_zeni, max_per_user, description, active) VALUES
  ('signup',           100, 1,  'Phần thưởng đăng ký Zeni Cloud lần đầu', TRUE),
  ('referral',         200, 50, 'Phần thưởng giới thiệu người dùng mới (mỗi người)', TRUE),
  ('first_payment',    500, 1,  'Phần thưởng cho lần thanh toán đầu tiên', TRUE),
  ('ai_optin',         100, 1,  'Phần thưởng opt-in chia sẻ data train AI', TRUE),
  ('monthly_loyalty',  50,  12, 'Phần thưởng loyalty hàng tháng (tối đa 12 tháng)', TRUE)
ON CONFLICT (action) DO NOTHING;

-- ─── Seed: Initial exchange rate (1 ZENI ≈ 25,000 VND ≈ 1 USD baseline) ─────
INSERT INTO token_exchange_rates (base_currency, quote_currency, rate, rate_usd, source)
VALUES ('ZENI', 'VND', 25000, 1.00, 'manual')
ON CONFLICT DO NOTHING;
