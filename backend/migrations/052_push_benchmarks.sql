-- Migration 052 — Push Notification Gateway (P0#6) + Benchmark Tracker (P1#10)

-- ═══════════════════════════════════════════════════════════════════════
-- PUSH NOTIFICATION (APNs + FCM)
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS push_devices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(128),                     -- customer's user_id (not Zeni user_id)
  device_token TEXT NOT NULL,
  platform VARCHAR(20) NOT NULL,            -- ios | android | web (FCM web)
  app_bundle_id VARCHAR(120),               -- com.clawwits.app
  device_locale VARCHAR(10),                -- vi-VN, en-US
  device_model VARCHAR(60),
  app_version VARCHAR(30),
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(workspace_id, device_token)
);

CREATE INDEX IF NOT EXISTS idx_push_devices_user ON push_devices(workspace_id, user_id) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_push_devices_platform ON push_devices(workspace_id, platform) WHERE enabled = TRUE;

CREATE TABLE IF NOT EXISTS push_notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  -- Targeting
  user_ids JSONB DEFAULT '[]'::jsonb,        -- ["uid1", "uid2"] OR empty = broadcast
  device_ids JSONB DEFAULT '[]'::jsonb,      -- specific device UUIDs
  platform_filter VARCHAR(20),               -- ios | android | web | NULL = all
  -- Content
  title VARCHAR(200),
  body TEXT,
  payload JSONB DEFAULT '{}'::jsonb,         -- custom data, e.g. {"deep_link": "...", "type": "..."}
  badge_count INT,
  sound VARCHAR(60) DEFAULT 'default',
  -- Result
  status VARCHAR(20) DEFAULT 'queued',
  total_devices INT DEFAULT 0,
  delivered_count INT DEFAULT 0,
  failed_count INT DEFAULT 0,
  errors JSONB DEFAULT '[]'::jsonb,
  apns_response JSONB,
  fcm_response JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  sent_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_push_notif_ws ON push_notifications(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS push_credentials (
  workspace_id VARCHAR(64) NOT NULL,
  platform VARCHAR(20) NOT NULL,             -- ios | android
  -- iOS APNs
  apns_team_id VARCHAR(20),
  apns_key_id VARCHAR(20),
  apns_p8_secret_id VARCHAR(120),            -- ref to identity vault secret
  apns_bundle_id VARCHAR(120),
  apns_environment VARCHAR(20) DEFAULT 'production', -- production | sandbox
  -- Android FCM
  fcm_project_id VARCHAR(120),
  fcm_service_account_secret_id VARCHAR(120),
  -- Audit
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (workspace_id, platform)
);

-- ═══════════════════════════════════════════════════════════════════════
-- BENCHMARK TRACKER (P1#10) — for Professor Wits AI CIO
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS benchmark_models (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  benchmark_name VARCHAR(60) NOT NULL,       -- swe-bench | humaneval | gpqa | aime | mmlu | lmsys-arena
  model_name VARCHAR(120) NOT NULL,
  model_provider VARCHAR(60),                -- anthropic | openai | google | meta | deepseek | xai | mistral
  model_version VARCHAR(60),
  score_value NUMERIC(8,4) NOT NULL,
  score_unit VARCHAR(20) DEFAULT 'percent',  -- percent | elo | accuracy
  rank INT,
  source_url TEXT,
  measured_at DATE NOT NULL,                 -- date of leaderboard snapshot
  recorded_at TIMESTAMPTZ DEFAULT NOW(),
  metadata JSONB DEFAULT '{}'::jsonb,
  UNIQUE(benchmark_name, model_name, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_bench_name_date ON benchmark_models(benchmark_name, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_bench_model ON benchmark_models(model_name, measured_at DESC);

-- Pre-seed top benchmarks list (config table)
CREATE TABLE IF NOT EXISTS benchmark_sources (
  id VARCHAR(60) PRIMARY KEY,
  display_name VARCHAR(100),
  description TEXT,
  source_url TEXT,
  scrape_schedule_cron VARCHAR(40) DEFAULT '0 7 * * *',
  is_active BOOLEAN DEFAULT TRUE,
  last_scraped_at TIMESTAMPTZ
);

INSERT INTO benchmark_sources (id, display_name, description, source_url) VALUES
  ('swe-bench',    'SWE-bench (software engineering)', 'Repository-level code editing benchmark', 'https://www.swebench.com/'),
  ('humaneval',    'HumanEval (code generation)',     'Functional correctness of generated code', 'https://github.com/openai/human-eval'),
  ('gpqa',         'GPQA (graduate-level QA)',        'Google-proof PhD-level science questions', 'https://github.com/idavidrein/gpqa'),
  ('aime',         'AIME (competition math)',         'American Invitational Math Exam',          'https://artofproblemsolving.com/wiki/index.php/AIME'),
  ('mmlu',         'MMLU (multi-task language)',      '57 subject-area knowledge benchmark',      'https://github.com/hendrycks/test'),
  ('lmsys-arena',  'LMSYS Chatbot Arena (Elo)',       'Crowdsourced human-pref ranking',          'https://chat.lmsys.org/?leaderboard'),
  ('big-bench',    'BIG-Bench Hard',                  '23 challenging reasoning tasks',           'https://github.com/google/BIG-bench'),
  ('agentbench',   'AgentBench (agent capabilities)', 'Multi-environment agent evaluation',       'https://github.com/THUDM/AgentBench')
ON CONFLICT (id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  description = EXCLUDED.description,
  source_url = EXCLUDED.source_url;

COMMENT ON TABLE push_devices IS 'Registered devices for push notifications (iOS APNs + Android FCM + Web)';
COMMENT ON TABLE push_notifications IS 'Push notification send history with delivery results';
COMMENT ON TABLE push_credentials IS 'Per-workspace APNs + FCM credentials (refs to vault secrets)';
COMMENT ON TABLE benchmark_models IS 'AI model benchmark scores over time (Professor Wits CIO data)';
COMMENT ON TABLE benchmark_sources IS 'Tracked benchmark sources (8 default leaderboards)';
