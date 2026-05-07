-- Migration 050 — Zeni Edge Runtime
-- Sandboxed microVM execution: chay AI agent code (Computer Use, autonomous workflows)
-- trong VM cach ly cap doanh nghiep — KHONG can khach mua VM rieng.
--
-- Use cases:
--   - Claude Computer Use thay vi chay tren may khach (rui ro)
--   - AI agent crawl web / scrape data trong sandbox
--   - User-submitted code execution (sandboxed)
--   - Long-running automation (cron + agent loops)

CREATE TABLE IF NOT EXISTS edge_sandboxes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id UUID,
  -- Sandbox spec
  runtime_type VARCHAR(40) NOT NULL,         -- python | node | computer-use | playwright | shell
  base_image TEXT NOT NULL,
  cpu_millis INT DEFAULT 1000,               -- 1000m = 1 CPU
  memory_mb INT DEFAULT 1024,                -- 1 GB default
  disk_mb INT DEFAULT 4096,                  -- 4 GB ephemeral disk
  timeout_sec INT DEFAULT 600,               -- 10 min hard timeout
  network_policy VARCHAR(20) DEFAULT 'allow-public', -- allow-public | allow-list | deny-all
  network_allowlist JSONB DEFAULT '[]'::jsonb,
  -- State
  status VARCHAR(20) DEFAULT 'idle',         -- idle | running | stopped | failed | terminated
  cloud_run_job_id TEXT,                     -- Cloud Run Jobs execution ID
  -- I/O
  exec_command TEXT,                         -- shell command or script entry
  exec_args JSONB DEFAULT '[]'::jsonb,
  exec_env JSONB DEFAULT '{}'::jsonb,
  stdin_payload TEXT,
  stdout_log TEXT,
  stderr_log TEXT,
  exit_code INT,
  -- Tracking
  created_at TIMESTAMPTZ DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  cpu_seconds_used FLOAT DEFAULT 0,
  memory_peak_mb INT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_edge_sandboxes_workspace ON edge_sandboxes(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_edge_sandboxes_status ON edge_sandboxes(status) WHERE status IN ('idle','running');

-- Predefined sandbox runtimes
CREATE TABLE IF NOT EXISTS edge_runtimes (
  id VARCHAR(60) PRIMARY KEY,
  display_name VARCHAR(100) NOT NULL,
  base_image TEXT NOT NULL,
  default_cpu_millis INT DEFAULT 1000,
  default_memory_mb INT DEFAULT 1024,
  description TEXT,
  cost_per_second_credits NUMERIC(10,4) DEFAULT 0.0010,
  is_active BOOLEAN DEFAULT TRUE
);

INSERT INTO edge_runtimes (id, display_name, base_image, default_cpu_millis, default_memory_mb, cost_per_second_credits, description) VALUES
  ('python-3.12', 'Python 3.12 (data science)', 'python:3.12-slim', 1000, 2048, 0.0010,
   'Python 3.12 + numpy/pandas/scipy preinstalled. Cho data analysis, ML inference, AI agent code.'),
  ('node-20', 'Node.js 20 (server-side)', 'node:20-alpine', 1000, 1024, 0.0008,
   'Node 20 LTS. Cho TypeScript/JS execution, web scraping, automation scripts.'),
  ('computer-use', 'Claude Computer Use (full GUI)', 'gcr.io/zeni-cloud-core/edge/computer-use:latest', 2000, 4096, 0.0050,
   'Headless Linux desktop voi xdotool + screenshot + Anthropic Computer Use API. Browser, file manager, terminal — Claude tu dong dieu khien.'),
  ('playwright', 'Playwright (browser automation)', 'mcr.microsoft.com/playwright:latest', 2000, 2048, 0.0030,
   'Playwright voi Chromium/Firefox/WebKit headless. Cho scraping, E2E test, form automation.'),
  ('shell-ubuntu', 'Ubuntu Shell (general)', 'ubuntu:22.04', 500, 512, 0.0005,
   'Ubuntu 22.04 vanilla. Cho ad-hoc shell command, cron jobs, lightweight tasks.')
ON CONFLICT (id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  base_image = EXCLUDED.base_image,
  default_cpu_millis = EXCLUDED.default_cpu_millis,
  default_memory_mb = EXCLUDED.default_memory_mb,
  cost_per_second_credits = EXCLUDED.cost_per_second_credits,
  description = EXCLUDED.description;

-- Per-workspace quotas
CREATE TABLE IF NOT EXISTS edge_runtime_quotas (
  workspace_id VARCHAR(64) PRIMARY KEY,
  max_concurrent INT DEFAULT 3,
  max_seconds_per_month INT DEFAULT 18000,   -- 5 hours/month free tier
  used_seconds_this_month INT DEFAULT 0,
  reset_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '1 month')
);

COMMENT ON TABLE edge_sandboxes IS 'Zeni Edge Runtime: sandboxed microVMs cho AI agent / automation execution';
COMMENT ON TABLE edge_runtimes IS 'Predefined runtime images: Python, Node, Computer Use, Playwright, Shell';
