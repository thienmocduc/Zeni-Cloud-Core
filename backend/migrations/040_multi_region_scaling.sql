-- ============================================================================
-- Migration 040 — Multi-Region Deployment + Auto-Scaling Policies (Sprint A5)
--
-- Purpose: Mở rộng Zeni Cloud Compute (L1) từ single-region (us-central1)
-- sang multi-region cho khách enterprise (Singapore, Tokyo, Belgium, US East...).
-- Đồng thời thêm tầng Auto-Scaling policies (CPU/memory/RPS/queue/schedule)
-- và Traffic Routing (geo / percent / canary / blue-green).
--
-- Tables (6):
--   1. regions                — Danh mục regions GCP, latency, tier availability
--   2. project_deployments    — Bản ghi deploy 1 project lên 1 region (multi-row/proj)
--   3. traffic_policies       — Geo-based / percent-based / canary / blue-green
--   4. scaling_policies       — Auto-scale theo CPU/RAM/RPS/queue depth/schedule
--   5. scaling_events         — Lịch sử scale up/down (debug + dashboard)
--   6. health_check_results   — Kết quả probe HTTP định kỳ cho từng region
--
-- An toàn:
--   * project_id REFERENCES projects(id) ON DELETE CASCADE — khi xoá project
--     toàn bộ deployment / policy / event tự huỷ.
--   * UNIQUE(project_id, region_id) trên project_deployments → 1 project chỉ
--     có tối đa 1 service trên 1 region (idempotent redeploy).
--   * traffic_percent CHECK (0..100); SUM enforce ở application layer.
-- ============================================================================

-- ─── 1. Regions catalog ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS regions (
    id                    SERIAL PRIMARY KEY,
    code                  VARCHAR(48) NOT NULL UNIQUE,           -- 'us-central1','asia-southeast1'
    name                  VARCHAR(128) NOT NULL,                 -- 'US Central (Iowa)'
    country               VARCHAR(8) NOT NULL,                   -- 'US','SG','JP','BE'
    gcp_region            VARCHAR(48) NOT NULL,                  -- exact GCP location string
    latency_ms_from_vn    INTEGER NOT NULL DEFAULT 200,          -- avg ping from VN endpoint
    available_for_tier    TEXT[] NOT NULL DEFAULT ARRAY['enterprise']::TEXT[],
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_regions_code      ON regions(code);
CREATE INDEX IF NOT EXISTS idx_regions_enabled   ON regions(enabled, latency_ms_from_vn);


-- ─── 2. Project Deployments (1 row = 1 project × 1 region) ──────────────────
CREATE TABLE IF NOT EXISTS project_deployments (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id               UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    region_id                INTEGER NOT NULL REFERENCES regions(id) ON DELETE RESTRICT,
    cloud_run_service_url    VARCHAR(512),                                  -- Cloud Run URL of this region
    cloud_run_service_name   VARCHAR(128),
    revision                 VARCHAR(64),
    status                   VARCHAR(24) NOT NULL DEFAULT 'pending',        -- pending/deploying/running/failed/draining
    traffic_percent          INTEGER NOT NULL DEFAULT 100
                             CHECK (traffic_percent BETWEEN 0 AND 100),
    deployed_at              TIMESTAMPTZ,
    deployed_by              VARCHAR(255),                                  -- user email
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, region_id)
);
CREATE INDEX IF NOT EXISTS idx_proj_deploy_project   ON project_deployments(project_id, status);
CREATE INDEX IF NOT EXISTS idx_proj_deploy_region    ON project_deployments(region_id, status);
CREATE INDEX IF NOT EXISTS idx_proj_deploy_status    ON project_deployments(status, created_at DESC);


-- ─── 3. Traffic Policies (routing rules) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS traffic_policies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    policy_type     VARCHAR(24) NOT NULL                                   -- 'geo','percent','canary','blue_green'
                    CHECK (policy_type IN ('geo','percent','canary','blue_green')),
    routing_rules   JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- For 'geo':       {"VN":"asia-southeast1","JP":"asia-northeast1","*":"us-central1"}
    -- For 'percent':   {"asia-southeast1":50,"us-central1":50}
    -- For 'canary':    {"stable_region":"us-central1","canary_region":"asia-southeast1","canary_percent":10,"ramp":[{"at":"+1h","pct":25},...]}
    -- For 'blue_green':{"blue":"us-central1","green":"asia-southeast1","active":"blue"}
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by      VARCHAR(255)
);
CREATE INDEX IF NOT EXISTS idx_traffic_policies_project   ON traffic_policies(project_id, active);
CREATE INDEX IF NOT EXISTS idx_traffic_policies_type      ON traffic_policies(policy_type, active);


-- ─── 4. Scaling Policies (auto-scale rules per project × region) ────────────
CREATE TABLE IF NOT EXISTS scaling_policies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id          UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    region_id           INTEGER REFERENCES regions(id) ON DELETE CASCADE,    -- NULL = all regions
    policy_type         VARCHAR(24) NOT NULL                                   -- 'cpu','memory','rps','queue_depth','schedule'
                        CHECK (policy_type IN ('cpu','memory','rps','queue_depth','schedule')),
    threshold_value     NUMERIC(12,2),                                         -- 70.0 for CPU%, 100.0 for RPS, etc.
    scale_up_step       INTEGER NOT NULL DEFAULT 1,                            -- +N instances when triggered
    scale_down_step     INTEGER NOT NULL DEFAULT 1,                            -- -N instances when reverse
    min_instances       INTEGER NOT NULL DEFAULT 0,
    max_instances       INTEGER NOT NULL DEFAULT 10,
    cooldown_seconds    INTEGER NOT NULL DEFAULT 60,                           -- prevent flap
    cron_schedule       VARCHAR(64),                                           -- only for policy_type='schedule' — '0 9 * * 1-5'
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by          VARCHAR(255),
    CHECK (min_instances >= 0 AND max_instances >= min_instances),
    CHECK (scale_up_step >= 1 AND scale_down_step >= 1)
);
CREATE INDEX IF NOT EXISTS idx_scaling_policies_project    ON scaling_policies(project_id, enabled);
CREATE INDEX IF NOT EXISTS idx_scaling_policies_region     ON scaling_policies(region_id, enabled);
CREATE INDEX IF NOT EXISTS idx_scaling_policies_type       ON scaling_policies(policy_type, enabled);


-- ─── 5. Scaling Events (audit trail of scale up/down) ───────────────────────
CREATE TABLE IF NOT EXISTS scaling_events (
    id                    BIGSERIAL PRIMARY KEY,
    project_id            UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    region_id             INTEGER REFERENCES regions(id) ON DELETE SET NULL,
    policy_id             UUID REFERENCES scaling_policies(id) ON DELETE SET NULL,
    event_type            VARCHAR(24) NOT NULL                                   -- 'scale_up','scale_down','throttle','no_change'
                          CHECK (event_type IN ('scale_up','scale_down','throttle','no_change')),
    trigger_metric        VARCHAR(48),                                           -- 'cpu','memory','rps','queue_depth','schedule','manual'
    trigger_value         NUMERIC(12,2),                                         -- the metric reading that fired the rule
    instances_before      INTEGER NOT NULL DEFAULT 0,
    instances_after       INTEGER NOT NULL DEFAULT 0,
    reason                TEXT,
    occurred_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scaling_events_project    ON scaling_events(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_scaling_events_region     ON scaling_events(region_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_scaling_events_type       ON scaling_events(event_type, occurred_at DESC);


-- ─── 6. Health Check Results (per-region probes) ────────────────────────────
CREATE TABLE IF NOT EXISTS health_check_results (
    id              BIGSERIAL PRIMARY KEY,
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    region_id       INTEGER NOT NULL REFERENCES regions(id) ON DELETE CASCADE,
    deployment_id   UUID REFERENCES project_deployments(id) ON DELETE CASCADE,
    status_code     INTEGER,                                       -- 200, 503, etc.
    latency_ms      INTEGER,
    healthy         BOOLEAN NOT NULL DEFAULT FALSE,
    error_message   TEXT,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_health_project    ON health_check_results(project_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_region     ON health_check_results(project_id, region_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_unhealthy  ON health_check_results(checked_at DESC) WHERE healthy = FALSE;


-- ─── Seed regions ───────────────────────────────────────────────────────────
INSERT INTO regions (code, name, country, gcp_region, latency_ms_from_vn, available_for_tier) VALUES
    ('us-central1',     'US Central (Iowa)',       'US', 'us-central1',     220, ARRAY['free','starter','pro','business','enterprise']),
    ('asia-southeast1', 'Singapore',               'SG', 'asia-southeast1',  50, ARRAY['starter','pro','business','enterprise']),
    ('asia-northeast1', 'Tokyo',                   'JP', 'asia-northeast1',  80, ARRAY['pro','business','enterprise']),
    ('europe-west1',    'Belgium',                 'BE', 'europe-west1',    250, ARRAY['business','enterprise']),
    ('us-east1',        'US East (S. Carolina)',   'US', 'us-east1',        230, ARRAY['business','enterprise'])
ON CONFLICT (code) DO NOTHING;


-- ─── updated_at trigger helpers ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION trg_set_updated_at_040() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_proj_deploy_updated   ON project_deployments;
CREATE TRIGGER trg_proj_deploy_updated   BEFORE UPDATE ON project_deployments
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_040();

DROP TRIGGER IF EXISTS trg_traffic_policies_updated ON traffic_policies;
CREATE TRIGGER trg_traffic_policies_updated BEFORE UPDATE ON traffic_policies
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_040();

DROP TRIGGER IF EXISTS trg_scaling_policies_updated ON scaling_policies;
CREATE TRIGGER trg_scaling_policies_updated BEFORE UPDATE ON scaling_policies
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_040();


-- ─── Migration log row (best-effort) ────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'migration_log') THEN
        INSERT INTO migration_log(version, description, applied_at)
        VALUES ('040', 'Multi-Region Deployment + Auto-Scaling Policies', NOW())
        ON CONFLICT DO NOTHING;
    END IF;
END $$;
