-- Migration 049 — Zeni Build Farm
-- Cloud build service cho native apps (Tauri / Rust / Electron / Go binary / Flutter / .NET)
-- Khach upload source -> Build Farm chay rustc/cargo/MSVC/Xcode trong cloud -> tra binary (.exe / .dmg / .deb / .AppImage / .apk / .ipa)
-- Khach KHONG can cai dat toolchain local

CREATE TABLE IF NOT EXISTS build_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id UUID,
  -- Build config
  job_type VARCHAR(40) NOT NULL,             -- tauri | rust | electron | go | flutter | dotnet
  source_type VARCHAR(20) NOT NULL,          -- zip | github | gcs
  source_ref TEXT NOT NULL,                  -- gs://... | github://owner/repo@branch
  target_platforms JSONB DEFAULT '["linux-x64"]'::jsonb,  -- linux-x64, windows-x64, macos-x64, macos-arm64, android-arm64, ios-arm64
  build_config JSONB DEFAULT '{}'::jsonb,    -- ENV vars, build args, signing certs ref
  -- Status
  status VARCHAR(20) DEFAULT 'queued',       -- queued | running | success | failed | cancelled
  cloudbuild_op_id TEXT,                     -- Cloud Build operation ID
  artifact_urls JSONB DEFAULT '[]'::jsonb,   -- [{platform, gcs_path, signed_url, size_bytes, sha256}]
  error_message TEXT,
  -- Tracking
  created_at TIMESTAMPTZ DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,                    -- artifacts auto-cleanup after 30 days
  -- Cost
  cost_credits INT DEFAULT 0,                -- billing units
  build_duration_sec INT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_build_jobs_workspace ON build_jobs(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_build_jobs_status ON build_jobs(status) WHERE status IN ('queued', 'running');

-- Build templates: predefined toolchain images for fast cold-start
CREATE TABLE IF NOT EXISTS build_farm_toolchains (
  id VARCHAR(60) PRIMARY KEY,
  display_name VARCHAR(100) NOT NULL,
  base_image TEXT NOT NULL,                  -- gcr.io/zeni-cloud-core/build-farm/tauri:latest
  supported_targets JSONB NOT NULL,          -- ["linux-x64", "windows-x64", "macos-x64"]
  default_build_args JSONB DEFAULT '{}'::jsonb,
  cost_per_minute_credits INT DEFAULT 10,
  estimated_duration_sec INT DEFAULT 300,
  description TEXT,
  is_active BOOLEAN DEFAULT TRUE
);

INSERT INTO build_farm_toolchains (id, display_name, base_image, supported_targets, cost_per_minute_credits, estimated_duration_sec, description) VALUES
  ('tauri-latest', 'Tauri 2.x (Rust + Webview)', 'gcr.io/zeni-cloud-core/build-farm/tauri:2',
   '["linux-x64","windows-x64"]'::jsonb, 12, 480,
   'Tauri 2.x voi Rust 1.80+ + Node 20 + cross-rs. Build .exe va .AppImage cung luc. KHONG can rustc local.'),
  ('rust-stable', 'Rust 1.80+ (cross-compile)', 'gcr.io/zeni-cloud-core/build-farm/rust:stable',
   '["linux-x64","linux-arm64","windows-x64","macos-x64"]'::jsonb, 10, 360,
   'Pure Rust binary build voi cross compilation. CLI tools, microservices, system daemons.'),
  ('electron-builder', 'Electron Builder', 'gcr.io/zeni-cloud-core/build-farm/electron:latest',
   '["linux-x64","windows-x64","macos-x64"]'::jsonb, 8, 420,
   'Electron app -> .exe / .dmg / .deb / .AppImage. Auto code-signing voi Zeni Vault cert.'),
  ('go-modules', 'Go 1.23+ (multi-platform)', 'gcr.io/zeni-cloud-core/build-farm/go:1.23',
   '["linux-x64","linux-arm64","windows-x64","macos-x64","macos-arm64"]'::jsonb, 5, 180,
   'Go static binary. CGO_ENABLED=0 cho minimum size. CLI tools, network services.'),
  ('flutter-stable', 'Flutter (Mobile + Desktop)', 'gcr.io/zeni-cloud-core/build-farm/flutter:stable',
   '["android-arm64","ios-arm64","linux-x64","windows-x64","macos-x64"]'::jsonb, 15, 600,
   'Flutter SDK voi Android NDK + iOS toolchain (mac-only). APK/IPA + desktop binaries.'),
  ('dotnet-8', '.NET 8 (cross-platform)', 'gcr.io/zeni-cloud-core/build-farm/dotnet:8',
   '["linux-x64","windows-x64","macos-x64"]'::jsonb, 8, 240,
   '.NET 8 self-contained single-file publish. CLI, services, MAUI desktop.')
ON CONFLICT (id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  base_image = EXCLUDED.base_image,
  supported_targets = EXCLUDED.supported_targets,
  cost_per_minute_credits = EXCLUDED.cost_per_minute_credits,
  estimated_duration_sec = EXCLUDED.estimated_duration_sec,
  description = EXCLUDED.description;

-- Quotas: limit concurrent builds per workspace
CREATE TABLE IF NOT EXISTS build_farm_quotas (
  workspace_id VARCHAR(64) PRIMARY KEY,
  max_concurrent INT DEFAULT 2,              -- free tier: 2 concurrent builds
  max_minutes_per_month INT DEFAULT 500,    -- free tier: 500 build-minutes/month
  used_minutes_this_month INT DEFAULT 0,
  reset_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '1 month')
);

COMMENT ON TABLE build_jobs IS 'Zeni Build Farm: cloud build jobs cho native apps (Tauri/Rust/Electron/Go/Flutter/.NET)';
COMMENT ON TABLE build_farm_toolchains IS 'Predefined toolchain images. Khach pick 1 -> Build Farm pull image va run build.';
COMMENT ON TABLE build_farm_quotas IS 'Per-workspace quotas: concurrent + monthly build minutes.';
