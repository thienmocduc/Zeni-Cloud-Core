-- Migration 075 — Workspace AI Quotas (tracking + enforcement per month)
-- Em commit theo plan VC Vertex-only ($0 tươi, 100% Zeni Cloud).
-- Track monthly usage cho 4 buckets: reasoning tokens, vision tokens, image renders, storage.

CREATE TABLE IF NOT EXISTS workspace_ai_quotas (
    workspace_id            VARCHAR(64)  NOT NULL,
    period_month            VARCHAR(7)   NOT NULL,           -- '2026-05' YYYY-MM
    reasoning_tokens_used   BIGINT       NOT NULL DEFAULT 0,
    reasoning_tokens_quota  BIGINT       NOT NULL DEFAULT 0, -- 0 = unlimited
    vision_tokens_used      BIGINT       NOT NULL DEFAULT 0,
    vision_tokens_quota     BIGINT       NOT NULL DEFAULT 0,
    image_count_used        INT          NOT NULL DEFAULT 0,
    image_count_quota       INT          NOT NULL DEFAULT 0,
    storage_gb_used         NUMERIC(10, 3) NOT NULL DEFAULT 0,
    storage_gb_quota        NUMERIC(10, 3) NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, period_month)
);

CREATE INDEX IF NOT EXISTS idx_ws_ai_quotas_ws ON workspace_ai_quotas(workspace_id);

COMMENT ON TABLE  workspace_ai_quotas IS 'Per-workspace per-month AI quota tracking (Vertex AI: Gemini + Imagen)';
COMMENT ON COLUMN workspace_ai_quotas.reasoning_tokens_quota IS 'Quota Gemini Pro reasoning tokens/tháng (0 = unlimited)';
COMMENT ON COLUMN workspace_ai_quotas.vision_tokens_quota IS 'Quota Gemini Pro vision/multimodal tokens/tháng';
COMMENT ON COLUMN workspace_ai_quotas.image_count_quota IS 'Quota Imagen 3 image renders/tháng';
COMMENT ON COLUMN workspace_ai_quotas.storage_gb_quota IS 'Quota GCS storage GB/tháng';

-- Insert Viet Contech quota Phase 1 (2026-05)
INSERT INTO workspace_ai_quotas (
    workspace_id, period_month,
    reasoning_tokens_quota, vision_tokens_quota,
    image_count_quota, storage_gb_quota
) VALUES (
    'vietcontech', '2026-05',
    10000000,    -- 10M tokens reasoning Gemini Pro
    5000000,     -- 5M tokens vision Gemini Pro
    2000,        -- 2K images Imagen 3
    10.0         -- 10GB GCS storage
)
ON CONFLICT (workspace_id, period_month) DO UPDATE
    SET reasoning_tokens_quota = EXCLUDED.reasoning_tokens_quota,
        vision_tokens_quota   = EXCLUDED.vision_tokens_quota,
        image_count_quota     = EXCLUDED.image_count_quota,
        storage_gb_quota      = EXCLUDED.storage_gb_quota,
        updated_at            = NOW();
