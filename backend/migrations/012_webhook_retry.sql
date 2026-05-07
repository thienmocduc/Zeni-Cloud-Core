-- ═══════════════════════════════════════════════════════════
-- A4: Webhook retry queue + Dead Letter Queue (DLQ)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS webhook_attempts (
  id              BIGSERIAL    PRIMARY KEY,
  workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  connector_id    UUID         REFERENCES connectors(id) ON DELETE SET NULL,
  source          VARCHAR(64)  NOT NULL,
  action          VARCHAR(64)  NOT NULL,
  target_url      VARCHAR(512) NOT NULL,
  payload         JSONB        NOT NULL,
  headers         JSONB        DEFAULT '{}'::jsonb,
  -- Retry state
  attempt_count   INTEGER      NOT NULL DEFAULT 0,
  max_attempts    INTEGER      NOT NULL DEFAULT 5,
  status          VARCHAR(16)  NOT NULL DEFAULT 'pending',  -- pending | succeeded | failed | dlq
  last_status_code INTEGER,
  last_error      TEXT,
  last_response   TEXT,
  -- Scheduling
  next_attempt_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  first_attempted_at TIMESTAMPTZ,
  succeeded_at    TIMESTAMPTZ,
  dlq_at          TIMESTAMPTZ,
  -- Audit
  actor           VARCHAR(255),
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_webhook_due       ON webhook_attempts(next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_webhook_ws_status ON webhook_attempts(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_dlq       ON webhook_attempts(status, dlq_at) WHERE status = 'dlq';

GRANT ALL PRIVILEGES ON webhook_attempts TO zeni_app;
GRANT USAGE, SELECT ON webhook_attempts_id_seq TO zeni_app;
