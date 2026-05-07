-- ═══════════════════════════════════════════════════════════
-- Pricing & Billing v2: wallet credits + subscription tiers
-- Markup model: GIÁ KHÁCH = COST × markup_ratio (default 4x)
-- ═══════════════════════════════════════════════════════════

-- Wallet (prepaid credits) per workspace
CREATE TABLE IF NOT EXISTS wallet_balances (
  workspace_id    VARCHAR(32)   PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
  balance_vnd     NUMERIC(14,2) NOT NULL DEFAULT 0,
  total_topped_up NUMERIC(14,2) NOT NULL DEFAULT 0,
  total_spent     NUMERIC(14,2) NOT NULL DEFAULT 0,
  last_charged_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Subscription per workspace (one active sub at a time)
CREATE TABLE IF NOT EXISTS subscriptions (
  id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  tier            VARCHAR(32)  NOT NULL,    -- 'free' | 'starter' | 'pro' | 'business' | 'enterprise'
  price_vnd_month NUMERIC(12,2) NOT NULL,
  -- monthly quota (resets on billing cycle)
  quota_agent_runs       INTEGER NOT NULL DEFAULT 0,
  quota_image_renders    INTEGER NOT NULL DEFAULT 0,
  quota_text_tokens_out  BIGINT  NOT NULL DEFAULT 0,
  -- usage in current period
  used_agent_runs        INTEGER NOT NULL DEFAULT 0,
  used_image_renders     INTEGER NOT NULL DEFAULT 0,
  used_text_tokens_out   BIGINT  NOT NULL DEFAULT 0,
  -- billing cycle
  period_start    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  period_end      TIMESTAMPTZ  NOT NULL,
  status          VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active|paused|cancelled
  auto_renew      BOOLEAN      NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  cancelled_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_ws_active ON subscriptions(workspace_id, status) WHERE status='active';

-- Wallet transactions (audit trail mọi top-up / charge)
CREATE TABLE IF NOT EXISTS wallet_transactions (
  id            BIGSERIAL    PRIMARY KEY,
  workspace_id  VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind          VARCHAR(16)  NOT NULL,  -- 'topup' | 'charge' | 'refund' | 'sub_payment'
  amount_vnd    NUMERIC(14,2) NOT NULL, -- positive=in (topup), negative=out (charge)
  balance_after NUMERIC(14,2) NOT NULL,
  cost_usd      NUMERIC(14,8),          -- raw GCP cost for this charge (if applicable)
  description   VARCHAR(255),
  ref_id        VARCHAR(64),            -- e.g., agent run ID, top-up payment ID
  actor         VARCHAR(255),
  metadata      JSONB        DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_ws_date ON wallet_transactions(workspace_id, created_at DESC);

-- Price book: markup config (admin-tunable)
CREATE TABLE IF NOT EXISTS price_book (
  product_key   VARCHAR(64)   PRIMARY KEY,
  display_name  VARCHAR(128)  NOT NULL,
  cost_usd_per_unit NUMERIC(12,8) NOT NULL,  -- raw GCP cost
  markup_ratio  NUMERIC(6,3)  NOT NULL DEFAULT 4.0,  -- price = cost × ratio
  unit          VARCHAR(32)   NOT NULL,        -- 'run', 'image', 'token_1m_out', etc
  active        BOOLEAN       NOT NULL DEFAULT TRUE,
  updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- Seed price book (markup 4x default)
INSERT INTO price_book (product_key, display_name, cost_usd_per_unit, markup_ratio, unit) VALUES
  ('agent.run.no_render',    'Agent run (no image)',      0.034,  4.0, 'run'),
  ('agent.run.with_2_render','Agent run + 2 ảnh',         0.114,  4.0, 'run'),
  ('agent.refine.render_only','Refine render only',       0.080,  4.0, 'run'),
  ('imagen.image',           'Imagen 3 / image',          0.040,  4.0, 'image'),
  ('gemini.pro.tokens_1m_out','Gemini 2.5 Pro 1M tok out',10.00,  4.0, 'token_1m_out'),
  ('gemini.pro.tokens_1m_in', 'Gemini 2.5 Pro 1M tok in', 1.25,   4.0, 'token_1m_in'),
  ('gemini.flash.tokens_1m_out','Gemini Flash 1M tok out',2.50,   4.0, 'token_1m_out'),
  ('gemini.flash.tokens_1m_in','Gemini Flash 1M tok in',  0.30,   4.0, 'token_1m_in'),
  ('embed.tokens_1m',        'Embeddings 1M tokens',      0.025,  4.0, 'token_1m'),
  ('compute.deploy',         'Cloud Run deploy',          0.0001, 5.0, 'deploy'),
  ('data.query',             'SQL query',                 0.000012,5.0,'query')
ON CONFLICT (product_key) DO UPDATE SET
  cost_usd_per_unit = EXCLUDED.cost_usd_per_unit,
  display_name      = EXCLUDED.display_name,
  unit              = EXCLUDED.unit,
  updated_at        = NOW();

-- Seed default free trial wallet for existing workspaces (50K VND each = ~$2 free credits)
INSERT INTO wallet_balances (workspace_id, balance_vnd, total_topped_up)
  SELECT id, 50000, 50000 FROM workspaces
ON CONFLICT (workspace_id) DO NOTHING;

GRANT ALL PRIVILEGES ON wallet_balances TO zeni_app;
GRANT ALL PRIVILEGES ON subscriptions TO zeni_app;
GRANT ALL PRIVILEGES ON wallet_transactions TO zeni_app;
GRANT ALL PRIVILEGES ON price_book TO zeni_app;
GRANT USAGE, SELECT ON wallet_transactions_id_seq TO zeni_app;
