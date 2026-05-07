-- ═══════════════════════════════════════════════════════════
-- A2: Cache (KV with TTL) + Queue (Postgres SKIP LOCKED) — public schema
-- Cache là UNLOGGED để max throughput; Queue logged để durable.
-- ═══════════════════════════════════════════════════════════

-- ── Cache: Key-Value với TTL, UNLOGGED cho hiệu năng ────────
CREATE UNLOGGED TABLE IF NOT EXISTS public.kv_cache (
  workspace_id TEXT        NOT NULL,
  key          TEXT        NOT NULL,
  value        JSONB       NOT NULL,
  expires_at   TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (workspace_id, key)
);
CREATE INDEX IF NOT EXISTS idx_kv_cache_expires
  ON public.kv_cache(expires_at)
  WHERE expires_at IS NOT NULL;

-- ── Queue: jobs với SKIP LOCKED pattern ─────────────────────
CREATE TABLE IF NOT EXISTS public.queue_jobs (
  id            BIGSERIAL    PRIMARY KEY,
  workspace_id  TEXT         NOT NULL,
  queue_name    TEXT         NOT NULL,
  payload       JSONB        NOT NULL,
  status        TEXT         NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','leased','completed','failed','dead_letter')),
  attempts      INT          NOT NULL DEFAULT 0,
  max_attempts  INT          NOT NULL DEFAULT 3,
  available_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  leased_until  TIMESTAMPTZ,
  lease_token   UUID,
  last_error    TEXT,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  completed_at  TIMESTAMPTZ
);

-- Index cho pull (pending + available)
CREATE INDEX IF NOT EXISTS idx_queue_pull
  ON public.queue_jobs(workspace_id, queue_name, status, available_at)
  WHERE status = 'pending';

-- Index cho reclaim expired leases
CREATE INDEX IF NOT EXISTS idx_queue_leased
  ON public.queue_jobs(leased_until)
  WHERE status = 'leased';

-- ── Grants ──────────────────────────────────────────────────
GRANT ALL PRIVILEGES ON public.kv_cache    TO zeni_app;
GRANT ALL PRIVILEGES ON public.queue_jobs  TO zeni_app;
GRANT USAGE, SELECT ON public.queue_jobs_id_seq TO zeni_app;
