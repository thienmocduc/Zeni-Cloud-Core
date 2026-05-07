-- Migration 046 — Source ZIP Upload
-- Cho phép khách upload ZIP source code → Zeni build & deploy KHÔNG CẦN GITHUB

CREATE TABLE IF NOT EXISTS source_uploads (
  id              BIGSERIAL PRIMARY KEY,
  upload_id       TEXT UNIQUE NOT NULL,
  workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  -- Source
  file_size_bytes BIGINT NOT NULL,
  file_count      INT NOT NULL DEFAULT 0,
  gcs_path        TEXT,                                -- gs://bucket/uploads/{upload_id}.zip
  -- Framework
  framework       VARCHAR(32) NOT NULL DEFAULT 'auto',
  detected_framework VARCHAR(32),
  -- Project
  project_name    VARCHAR(48),
  project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
  -- Build
  build_id        TEXT,
  build_log       TEXT,
  image_url       TEXT,
  deploy_url      TEXT,
  -- State
  status          VARCHAR(20) NOT NULL DEFAULT 'queued',  -- queued|extracting|building|deploying|success|failed|cancelled
  error_message   TEXT,
  -- Audit
  uploaded_by     TEXT,
  uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_source_uploads_ws ON source_uploads (workspace_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_uploads_status ON source_uploads (status) WHERE status IN ('queued','extracting','building','deploying');
CREATE INDEX IF NOT EXISTS idx_source_uploads_uid ON source_uploads (upload_id);
