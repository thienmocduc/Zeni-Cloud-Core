-- ============================================================================
-- Migration 078 — Training Pipeline (datasets + jobs + deployed LoRA models)
--
-- Designed for the 650k image Vietcontech dataset @ gs://wellnexus-data-warehouse/
-- → SDXL/FLUX LoRA fine-tuning on Vertex AI Custom Training (A100 40GB)
-- → output weights to gs://witsagi-llm-lora/{job_id}/
-- → registered as `lora_models` row for /design/render-vietcontech-style.
--
-- See backend/app/api/training.py + backend/services/lora_train.py
-- ============================================================================

-- ─── 1. Datasets (GCS-backed) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS training_datasets (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      VARCHAR(64) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name              VARCHAR(128) NOT NULL,
    gcs_uri           TEXT NOT NULL,                  -- gs://bucket/prefix/
    format            VARCHAR(32) NOT NULL DEFAULT 'webdataset',  -- 'webdataset' | 'parquet' | 'imagefolder'
    image_count       INT NOT NULL DEFAULT 0,
    total_size_bytes  BIGINT NOT NULL DEFAULT 0,
    status            VARCHAR(20) NOT NULL DEFAULT 'ready',       -- 'ready' | 'ingesting' | 'deleted'
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_training_datasets_ws
    ON training_datasets(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_training_datasets_created
    ON training_datasets(created_at DESC);

-- ─── 2. Training jobs ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS training_jobs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      VARCHAR(64) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    dataset_id        UUID NOT NULL REFERENCES training_datasets(id),
    base_model        VARCHAR(32) NOT NULL,           -- 'sdxl' | 'flux1-dev'
    lora_rank         INT NOT NULL DEFAULT 16,        -- 8 | 16 | 32 | 64
    training_steps    INT NOT NULL DEFAULT 4000,
    learning_rate     DOUBLE PRECISION NOT NULL DEFAULT 1e-4,
    status            VARCHAR(20) NOT NULL DEFAULT 'queued',
        -- 'queued' | 'starting' | 'running' | 'succeeded' | 'failed' | 'cancelled'
    vertex_job_id     VARCHAR(128) NULL,              -- Vertex AI CustomJob resource name
    gcs_output_uri    TEXT NULL,                      -- gs://witsagi-llm-lora/{job_id}/
    started_at        TIMESTAMPTZ NULL,
    completed_at      TIMESTAMPTZ NULL,
    cost_usd          NUMERIC(10,2) NOT NULL DEFAULT 0,
    error_message     TEXT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_training_jobs_ws
    ON training_jobs(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_training_jobs_dataset
    ON training_jobs(dataset_id);
CREATE INDEX IF NOT EXISTS idx_training_jobs_vertex
    ON training_jobs(vertex_job_id) WHERE vertex_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_training_jobs_active
    ON training_jobs(status) WHERE status IN ('queued', 'starting', 'running');

-- ─── 3. Deployed LoRA models (consumed by /design/render-vietcontech-style) ─
CREATE TABLE IF NOT EXISTS lora_models (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        VARCHAR(64) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    training_job_id     UUID NULL REFERENCES training_jobs(id),
    name                VARCHAR(128) NOT NULL,
    gcs_weights_uri     TEXT NOT NULL,                -- gs://witsagi-llm-lora/{job_id}/pytorch_lora_weights.safetensors
    inference_endpoint  VARCHAR(255) NULL,            -- optional Cloud Run inference URL
    status              VARCHAR(20) NOT NULL DEFAULT 'deployed',
        -- 'deployed' | 'deploying' | 'paused' | 'deleted'
    use_count           INT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_lora_models_ws
    ON lora_models(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_lora_models_job
    ON lora_models(training_job_id) WHERE training_job_id IS NOT NULL;

-- ─── 4. Touch-on-update trigger (keep updated_at fresh) ─────────────────────
CREATE OR REPLACE FUNCTION _touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_training_datasets_touch ON training_datasets;
CREATE TRIGGER trg_training_datasets_touch
    BEFORE UPDATE ON training_datasets
    FOR EACH ROW EXECUTE FUNCTION _touch_updated_at();

DROP TRIGGER IF EXISTS trg_training_jobs_touch ON training_jobs;
CREATE TRIGGER trg_training_jobs_touch
    BEFORE UPDATE ON training_jobs
    FOR EACH ROW EXECUTE FUNCTION _touch_updated_at();
