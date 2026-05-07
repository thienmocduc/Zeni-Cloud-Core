-- Migration 058 — Package Registry npm-private + PyPI-private (P1#7 ClawWits)
-- Native npm + PyPI compatible registry, GCS-backed
-- Khach: `npm publish --registry=https://npm.zenicloud.io` + `twine upload --repository-url=https://pypi.zenicloud.io`

CREATE TABLE IF NOT EXISTS pkg_packages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  registry_type VARCHAR(20) NOT NULL,         -- npm | pypi
  -- Identity
  scope VARCHAR(80),                          -- @clawwits (npm only)
  name VARCHAR(180) NOT NULL,                 -- @clawwits/sdk-ts → scope=@clawwits, name=sdk-ts
  full_name VARCHAR(220) NOT NULL,            -- normalized: @clawwits/sdk-ts or pypi: clawwits-sdk
  -- Metadata
  description TEXT,
  homepage TEXT,
  repository TEXT,
  license VARCHAR(60),
  keywords JSONB DEFAULT '[]'::jsonb,
  authors JSONB DEFAULT '[]'::jsonb,
  -- Access
  visibility VARCHAR(20) DEFAULT 'private',   -- private | public-read | workspace-only
  allowed_workspace_ids JSONB DEFAULT '[]'::jsonb,
  -- Stats
  total_versions INT DEFAULT 0,
  total_downloads BIGINT DEFAULT 0,
  latest_version VARCHAR(60),
  -- Audit
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  created_by UUID,
  UNIQUE(registry_type, full_name)
);

CREATE INDEX IF NOT EXISTS idx_pkg_packages_ws ON pkg_packages(workspace_id);
CREATE INDEX IF NOT EXISTS idx_pkg_packages_full ON pkg_packages(full_name);

CREATE TABLE IF NOT EXISTS pkg_versions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  package_id UUID NOT NULL REFERENCES pkg_packages(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  version VARCHAR(60) NOT NULL,
  -- Tarball / wheel
  filename VARCHAR(220),
  gcs_path TEXT NOT NULL,                     -- gs://zeni-pkg-registry/...
  size_bytes BIGINT,
  -- Hashes
  sha512_b64 VARCHAR(120),                    -- npm uses sha512
  sha256_hex VARCHAR(80),                     -- pypi uses sha256
  md5_hex VARCHAR(40),
  -- Manifest
  package_json JSONB,                          -- for npm: full package.json
  pypi_metadata JSONB,                         -- for pypi: METADATA file parsed
  dependencies JSONB DEFAULT '{}'::jsonb,
  dev_dependencies JSONB DEFAULT '{}'::jsonb,
  peer_dependencies JSONB DEFAULT '{}'::jsonb,
  -- Audit
  published_by UUID,
  published_at TIMESTAMPTZ DEFAULT NOW(),
  yanked BOOLEAN DEFAULT FALSE,
  yank_reason TEXT,
  download_count BIGINT DEFAULT 0,
  UNIQUE(package_id, version)
);

CREATE INDEX IF NOT EXISTS idx_pkg_versions_pkg ON pkg_versions(package_id, published_at DESC);

-- Dist-tags (npm: latest, beta, next | pypi: stable)
CREATE TABLE IF NOT EXISTS pkg_dist_tags (
  package_id UUID NOT NULL REFERENCES pkg_packages(id) ON DELETE CASCADE,
  tag_name VARCHAR(40) NOT NULL,              -- "latest", "beta", "next"
  version VARCHAR(60) NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (package_id, tag_name)
);

-- Tokens for npm/pip publish auth (separate from JWT for CLI compatibility)
CREATE TABLE IF NOT EXISTS pkg_publish_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id UUID,
  token_hash VARCHAR(120) NOT NULL UNIQUE,
  token_prefix VARCHAR(20),                   -- "zeni_pkg_xyz" for display
  name VARCHAR(120),
  scopes JSONB DEFAULT '["read","write"]'::jsonb,
  expires_at TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  use_count BIGINT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pkg_tokens_hash ON pkg_publish_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_pkg_tokens_ws ON pkg_publish_tokens(workspace_id);

COMMENT ON TABLE pkg_packages IS 'Package registry: npm + pypi compatible';
COMMENT ON TABLE pkg_versions IS 'Published package versions with tarball/wheel in GCS';
COMMENT ON TABLE pkg_dist_tags IS 'Dist-tags eg npm: latest, beta';
COMMENT ON TABLE pkg_publish_tokens IS 'CLI publish tokens (npm/twine compatible)';
