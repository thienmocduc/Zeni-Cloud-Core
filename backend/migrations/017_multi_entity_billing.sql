-- ═══════════════════════════════════════════════════════════
-- A5: Multi-Entity Billing — Zeni Holdings + 5 pháp nhân con
--
-- Nguyên tắc:
--   1. Idempotent (CREATE IF NOT EXISTS, INSERT ON CONFLICT)
--   2. KHÔNG drop / break wallet_balances, wallet_transactions, billing_events
--   3. parent_id UPDATE chạy sau INSERT để tránh FK ordering
--   4. billing_transactions là ledger doanh thu tagged-by-entity
--      (song song wallet_transactions, không thay thế)
-- ═══════════════════════════════════════════════════════════

-- ── 1. Bảng pháp nhân ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.legal_entities (
  id           TEXT         PRIMARY KEY,                 -- 'zeni_holdings', 'anima_care', ...
  name         TEXT         NOT NULL,
  parent_id    TEXT         REFERENCES public.legal_entities(id),
  bank_account TEXT,
  tax_id       TEXT,
  is_master    BOOLEAN      NOT NULL DEFAULT FALSE,
  notes        TEXT,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── 2. billing_transactions — revenue ledger tagged by entity ──
-- Tạo IF NOT EXISTS để không phá nếu đã tồn tại từ migration khác.
CREATE TABLE IF NOT EXISTS public.billing_transactions (
  id              BIGSERIAL    PRIMARY KEY,
  workspace_id    VARCHAR(32)  REFERENCES public.workspaces(id) ON DELETE SET NULL,
  amount_vnd      NUMERIC(18,2) NOT NULL,
  action          VARCHAR(64)  NOT NULL,
  metadata        JSONB        NOT NULL DEFAULT '{}'::jsonb,
  actor           VARCHAR(255),
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── 3. Tag billing_transactions với pháp nhân ───────────────
ALTER TABLE public.billing_transactions
  ADD COLUMN IF NOT EXISTS legal_entity_id TEXT REFERENCES public.legal_entities(id);

CREATE INDEX IF NOT EXISTS idx_billing_tx_entity
  ON public.billing_transactions(legal_entity_id, created_at);

CREATE INDEX IF NOT EXISTS idx_billing_tx_ws_date
  ON public.billing_transactions(workspace_id, created_at DESC);

-- ── 4. Intercompany transfers (Zeni Holdings <-> con) ───────
CREATE TABLE IF NOT EXISTS public.intercompany_transfers (
  id            BIGSERIAL    PRIMARY KEY,
  from_entity   TEXT         NOT NULL REFERENCES public.legal_entities(id),
  to_entity     TEXT         NOT NULL REFERENCES public.legal_entities(id),
  amount_vnd    NUMERIC(18,2) NOT NULL CHECK (amount_vnd > 0),
  period_start  DATE         NOT NULL,
  period_end    DATE         NOT NULL,
  status        TEXT         NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','processed','failed','cancelled')),
  external_ref  TEXT,
  notes         TEXT,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  processed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_intercompany_period
  ON public.intercompany_transfers(period_start, period_end);

CREATE INDEX IF NOT EXISTS idx_intercompany_status
  ON public.intercompany_transfers(status, created_at);

-- ── 5. Map workspace → default legal_entity ────────────────
CREATE TABLE IF NOT EXISTS public.workspace_legal_entity (
  workspace_id     TEXT         PRIMARY KEY REFERENCES public.workspaces(id) ON DELETE CASCADE,
  legal_entity_id  TEXT         NOT NULL REFERENCES public.legal_entities(id),
  assigned_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── 6. Seed pháp nhân Zeni Holdings (parent_id NULL trước) ──
INSERT INTO public.legal_entities(id, name, is_master, notes) VALUES
  ('zeni_holdings', 'Zeni Holdings JSC', TRUE,  'Master entity — sở hữu master wallet, thu doanh thu tổng'),
  ('anima_care',    'ANIMA Care Co.',    FALSE, 'Wellness platform — Anima Care Global'),
  ('zeni_cloud',    'Zeni Cloud Co.',    FALSE, 'Cloud OS Vietnam — zenicloud.io'),
  ('ios_portal',    'IOS Portal Co.',    FALSE, 'Investor & Operations System'),
  ('zeni_chain',    'Zeni Chain Co.',    FALSE, 'Web3 Layer — Zeni Token + Affiliate'),
  ('zeniipo',       'Zeniipo Co.',       FALSE, 'IPO advisory + capital markets')
ON CONFLICT (id) DO NOTHING;

-- ── 7. Set parent_id sau khi tất cả rows đã insert ─────────
UPDATE public.legal_entities
   SET parent_id = 'zeni_holdings'
 WHERE id IN ('anima_care','zeni_cloud','ios_portal','zeni_chain','zeniipo')
   AND parent_id IS NULL;

-- ── 8. Grants ───────────────────────────────────────────────
GRANT ALL PRIVILEGES ON public.legal_entities          TO zeni_app;
GRANT ALL PRIVILEGES ON public.billing_transactions    TO zeni_app;
GRANT ALL PRIVILEGES ON public.intercompany_transfers  TO zeni_app;
GRANT ALL PRIVILEGES ON public.workspace_legal_entity  TO zeni_app;
GRANT USAGE, SELECT ON public.billing_transactions_id_seq   TO zeni_app;
GRANT USAGE, SELECT ON public.intercompany_transfers_id_seq TO zeni_app;
