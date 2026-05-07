-- ═══════════════════════════════════════════════════════════
-- L1 Compute REAL: add Cloud Run service tracking fields
-- ═══════════════════════════════════════════════════════════

ALTER TABLE projects ADD COLUMN IF NOT EXISTS image             VARCHAR(512);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS cloud_run_service VARCHAR(128);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS current_revision  VARCHAR(64);

-- Allow longer region string (was 32 char with default 'asia-southeast1')
-- No change needed since 'us-central1' fits in 32.

-- Unique constraint: one Cloud Run service name per project (case Cloud Run
-- service got renamed). Not enforced in DB to allow redeploy / rename flows.
CREATE INDEX IF NOT EXISTS idx_projects_cloud_run ON projects(cloud_run_service) WHERE cloud_run_service IS NOT NULL;

GRANT ALL PRIVILEGES ON projects TO zeni_app;
