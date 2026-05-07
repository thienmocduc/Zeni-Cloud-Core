-- Migration 055 — Zeni Storage (Supabase Storage parity)
-- S3-compatible object storage backed by Google Cloud Storage
-- Multi-tenant: bucket-per-workspace + per-bucket policies + signed URLs

CREATE TABLE IF NOT EXISTS storage_buckets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  name VARCHAR(120) NOT NULL,                 -- bucket logical name in workspace
  gcs_bucket_name VARCHAR(180) NOT NULL,      -- actual GCS bucket: zeni-storage-{ws}-{name}
  -- Access policy
  visibility VARCHAR(20) DEFAULT 'private',   -- private | public-read | authenticated
  allowed_mime_types JSONB DEFAULT '[]'::jsonb, -- empty = allow all; ["image/*","application/pdf"]
  max_file_size_mb INT DEFAULT 100,
  -- Quotas
  storage_quota_mb BIGINT DEFAULT 10240,      -- 10 GB default
  used_bytes BIGINT DEFAULT 0,
  -- Lifecycle
  default_expiry_days INT,                    -- NULL = never expire
  -- Versioning
  versioning_enabled BOOLEAN DEFAULT FALSE,
  -- Audit
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  created_by UUID,
  UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_storage_buckets_ws ON storage_buckets(workspace_id);

CREATE TABLE IF NOT EXISTS storage_objects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  bucket_id UUID NOT NULL REFERENCES storage_buckets(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  -- Object identity
  key TEXT NOT NULL,                          -- path/filename within bucket
  gcs_object_path TEXT NOT NULL,              -- actual gs:// path
  -- Metadata
  content_type VARCHAR(120),
  content_length BIGINT,
  etag VARCHAR(80),                           -- MD5/SHA hash
  custom_metadata JSONB DEFAULT '{}'::jsonb,
  -- Versioning
  version_id VARCHAR(80),
  is_latest BOOLEAN DEFAULT TRUE,
  -- Access
  uploaded_by UUID,
  uploaded_at TIMESTAMPTZ DEFAULT NOW(),
  last_accessed_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  -- Soft delete
  deleted_at TIMESTAMPTZ,
  UNIQUE(bucket_id, key, version_id)
);

CREATE INDEX IF NOT EXISTS idx_storage_objects_bucket ON storage_objects(bucket_id, key) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_storage_objects_ws ON storage_objects(workspace_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_storage_objects_expires ON storage_objects(expires_at) WHERE expires_at IS NOT NULL AND deleted_at IS NULL;

-- Signed URL audit (for compliance)
CREATE TABLE IF NOT EXISTS storage_signed_urls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  bucket_id UUID,
  object_key TEXT,
  url_method VARCHAR(10),                     -- GET | PUT | DELETE
  expires_at TIMESTAMPTZ,
  generated_by UUID,
  generated_at TIMESTAMPTZ DEFAULT NOW(),
  used_count INT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signed_urls_ws ON storage_signed_urls(workspace_id, generated_at DESC);

COMMENT ON TABLE storage_buckets IS 'Zeni Storage: S3-like buckets backed by GCS';
COMMENT ON TABLE storage_objects IS 'Storage objects metadata + GCS path';
COMMENT ON TABLE storage_signed_urls IS 'Signed URL audit log';
