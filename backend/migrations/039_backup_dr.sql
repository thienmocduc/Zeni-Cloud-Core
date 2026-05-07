-- ============================================================================
-- Migration 039 — Backup & Disaster Recovery (Sprint A7)
--
-- Purpose: Lớp Backup + DR cho khách của Zeni Cloud (PaaS).
--   * Khách lưu data trên Cloud SQL + GCS qua Zeni → Zeni chịu trách nhiệm
--     backup offsite, encryption (KMS), point-in-time recovery, và failover
--     đa-region khi region chính sự cố.
--   * Mỗi workspace có thể đặt nhiều backup_policies (cron schedule, retention,
--     scope: workspace | project | database | storage).
--   * backup_jobs: lịch sử backup runs (scheduled / manual / pre_migration).
--   * restore_jobs: yêu cầu khôi phục — mọi restore đều ghi audit (sensitive).
--   * backup_test_runs: integrity checks định kỳ (verify checksum + sample
--     restore) — bảo đảm backup không bị "thối".
--   * dr_sites: cấu hình DR multi-region (RTO/RPO + replication lag).
--
-- Tables (5):
--   1. backup_policies     — Khai báo policy backup (cron, retention, scope, KMS)
--   2. backup_jobs         — Lịch sử backup runs (status, gcs_uri, size, error)
--   3. restore_jobs        — Yêu cầu restore (selective scope, target ws)
--   4. backup_test_runs    — Test integrity định kỳ
--   5. dr_sites            — DR site config (primary/dr region, RTO/RPO)
--
-- An toàn:
--   * Tất cả tables có workspace_id (trừ dr_sites — admin-only).
--   * encryption_kms_key bắt buộc → KHÔNG cho backup không mã hoá.
--   * Restore action bắt buộc audit_push (sensitive).
--   * GCS uri lưu ở dạng gs://zeni-backups-{region}/{workspace}/{job_id}/
-- ============================================================================

-- ─── 1. Backup Policies ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backup_policies (
    id                  BIGSERIAL PRIMARY KEY,
    workspace_id        VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    schedule_cron       VARCHAR(64) NOT NULL,                                -- '0 2 * * *' (daily 2am UTC)
    retention_days      INTEGER NOT NULL DEFAULT 30 CHECK (retention_days BETWEEN 1 AND 3650),
    scope               VARCHAR(20) NOT NULL DEFAULT 'workspace'
                        CHECK (scope IN ('workspace','project','database','storage')),
    scope_target_id     VARCHAR(64),                                          -- project_id / db_id / bucket name; NULL when scope='workspace'
    encryption_kms_key  VARCHAR(255) NOT NULL,                                -- gcp KMS key resource name
    target_region       VARCHAR(32) NOT NULL DEFAULT 'us-central1',           -- offsite region
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at         TIMESTAMPTZ,
    next_run_at         TIMESTAMPTZ,
    created_by          VARCHAR(255),                                         -- email of user who created
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_backup_policies_ws
    ON backup_policies(workspace_id, enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_backup_policies_scheduler
    ON backup_policies(enabled, next_run_at) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_backup_policies_scope
    ON backup_policies(workspace_id, scope, scope_target_id);


-- ─── 2. Backup Jobs ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backup_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    workspace_id        VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    policy_id           BIGINT REFERENCES backup_policies(id) ON DELETE SET NULL,
    job_type            VARCHAR(20) NOT NULL DEFAULT 'scheduled'
                        CHECK (job_type IN ('scheduled','manual','pre_migration')),
    status              VARCHAR(16) NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','completed','failed','expired')),
    scope               VARCHAR(20) NOT NULL DEFAULT 'workspace'
                        CHECK (scope IN ('workspace','project','database','storage')),
    scope_target_id     VARCHAR(64),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    size_bytes          BIGINT NOT NULL DEFAULT 0,
    file_count          INTEGER NOT NULL DEFAULT 0,
    gcs_uri             VARCHAR(512),                                         -- gs://zeni-backups-.../job-{id}.tar.gz.enc
    encryption_status   VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (encryption_status IN ('pending','encrypted','failed','none')),
    encryption_kms_key  VARCHAR(255),
    checksum_sha256     VARCHAR(64),                                           -- integrity hash
    error_message       TEXT,
    triggered_by        VARCHAR(255),                                          -- email or 'cron' or 'pre-migration:0xxx'
    metadata            JSONB DEFAULT '{}'::jsonb,
    expires_at          TIMESTAMPTZ,                                           -- created_at + retention_days của policy
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_ws
    ON backup_jobs(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_status
    ON backup_jobs(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_policy
    ON backup_jobs(policy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_jobs_expiry
    ON backup_jobs(expires_at) WHERE status = 'completed' AND expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_backup_jobs_running
    ON backup_jobs(status, started_at) WHERE status IN ('queued','running');


-- ─── 3. Restore Jobs ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS restore_jobs (
    id                          BIGSERIAL PRIMARY KEY,
    workspace_id                VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    backup_id                   BIGINT REFERENCES backup_jobs(id) ON DELETE SET NULL,
    target_workspace_id         VARCHAR(32) REFERENCES workspaces(id) ON DELETE SET NULL,
    job_kind                    VARCHAR(16) NOT NULL DEFAULT 'restore'
                                CHECK (job_kind IN ('restore','pitr')),
    pitr_target_ts              TIMESTAMPTZ,                                  -- nullable for non-PITR restores
    scope                       JSONB NOT NULL DEFAULT '{}'::jsonb,           -- {tables:[..], projects:[..], buckets:[..]}
    status                      VARCHAR(16) NOT NULL DEFAULT 'queued'
                                CHECK (status IN ('queued','running','completed','failed','cancelled')),
    requested_by                VARCHAR(255) NOT NULL,                        -- email
    started_at                  TIMESTAMPTZ,
    completed_at                TIMESTAMPTZ,
    restored_records_count      BIGINT NOT NULL DEFAULT 0,
    restored_size_bytes         BIGINT NOT NULL DEFAULT 0,
    error_message               TEXT,
    metadata                    JSONB DEFAULT '{}'::jsonb,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_restore_jobs_ws
    ON restore_jobs(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_restore_jobs_status
    ON restore_jobs(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_restore_jobs_backup
    ON restore_jobs(backup_id, created_at DESC);


-- ─── 4. Backup Test Runs (integrity verification) ──────────────────────────
CREATE TABLE IF NOT EXISTS backup_test_runs (
    id                          BIGSERIAL PRIMARY KEY,
    policy_id                   BIGINT NOT NULL REFERENCES backup_policies(id) ON DELETE CASCADE,
    backup_id                   BIGINT REFERENCES backup_jobs(id) ON DELETE SET NULL,
    ran_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    integrity_check_passed      BOOLEAN NOT NULL DEFAULT FALSE,
    restore_test_passed         BOOLEAN NOT NULL DEFAULT FALSE,
    bytes_verified              BIGINT NOT NULL DEFAULT 0,
    duration_seconds            INTEGER NOT NULL DEFAULT 0,
    notes                       TEXT,
    error_message               TEXT,
    metadata                    JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_backup_tests_policy
    ON backup_test_runs(policy_id, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_tests_failed
    ON backup_test_runs(ran_at DESC)
    WHERE integrity_check_passed = FALSE OR restore_test_passed = FALSE;


-- ─── 5. DR Sites (admin-managed multi-region failover) ─────────────────────
CREATE TABLE IF NOT EXISTS dr_sites (
    id                          BIGSERIAL PRIMARY KEY,
    primary_region              VARCHAR(32) NOT NULL,                         -- 'us-central1'
    dr_region                   VARCHAR(32) NOT NULL,                         -- 'us-east1'
    replication_lag_seconds     INTEGER NOT NULL DEFAULT 0,
    last_failover_at            TIMESTAMPTZ,
    last_failback_at            TIMESTAMPTZ,
    status                      VARCHAR(20) NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','passive','maintenance','failed_over','failback_pending')),
    rto_seconds                 INTEGER NOT NULL DEFAULT 3600,                -- recovery time objective (1h default)
    rpo_seconds                 INTEGER NOT NULL DEFAULT 900,                 -- recovery point objective (15min)
    health_check_url            VARCHAR(512),
    notes                       TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (primary_region, dr_region)
);
CREATE INDEX IF NOT EXISTS idx_dr_sites_status
    ON dr_sites(status, primary_region);


-- ─── Seed: 1 baseline DR pair us-central1 ↔ us-east1 ───────────────────────
INSERT INTO dr_sites (primary_region, dr_region, replication_lag_seconds,
                      status, rto_seconds, rpo_seconds, notes)
VALUES ('us-central1', 'us-east1', 0, 'active', 3600, 900,
        'Baseline DR pair for Zeni Cloud — Cloud SQL cross-region replica + GCS dual-region.')
ON CONFLICT (primary_region, dr_region) DO NOTHING;


-- ─── Migration log row (best-effort) ───────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'migration_log') THEN
        INSERT INTO migration_log(version, description, applied_at)
        VALUES ('039', 'Backup & DR — policies/jobs/restore/tests/dr_sites', NOW())
        ON CONFLICT DO NOTHING;
    END IF;
END $$;
