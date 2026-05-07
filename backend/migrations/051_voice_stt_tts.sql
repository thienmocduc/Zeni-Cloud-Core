-- Migration 051 — Voice STT/TTS (P0#4-5 ClawWits)
-- Speech-to-text Whisper VN-tuned + Text-to-speech XTTS-v3 VN

CREATE TABLE IF NOT EXISTS voice_stt_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id UUID,
  audio_gcs_path TEXT NOT NULL,           -- gs://... where input audio lives
  audio_format VARCHAR(20),               -- mp3, wav, ogg, m4a, webm
  audio_duration_sec FLOAT,
  language VARCHAR(10) DEFAULT 'vi',      -- vi, en, auto
  model VARCHAR(40) DEFAULT 'whisper-vn', -- whisper-vn | whisper-large-v3 | whisper-medium
  status VARCHAR(20) DEFAULT 'queued',    -- queued | running | success | failed
  result_text TEXT,
  result_segments JSONB DEFAULT '[]'::jsonb,
  detected_language VARCHAR(10),
  confidence FLOAT,
  error_message TEXT,
  cost_credits NUMERIC(10,4) DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_stt_jobs_workspace ON voice_stt_jobs(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS voice_tts_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  user_id UUID,
  text_input TEXT NOT NULL,
  text_length INT,
  voice_id VARCHAR(60) DEFAULT 'vn-female-1', -- vn-female-1, vn-male-1, vn-child-1, en-female-1, etc.
  model VARCHAR(40) DEFAULT 'xtts-v3-vn',
  speed FLOAT DEFAULT 1.0,
  pitch FLOAT DEFAULT 0.0,
  format VARCHAR(20) DEFAULT 'mp3',       -- mp3, wav, opus
  output_gcs_path TEXT,                   -- gs://... where output lives after success
  output_duration_sec FLOAT,
  output_size_bytes INT,
  status VARCHAR(20) DEFAULT 'queued',
  error_message TEXT,
  cost_credits NUMERIC(10,4) DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tts_jobs_workspace ON voice_tts_jobs(workspace_id, created_at DESC);

-- Available voices catalog
CREATE TABLE IF NOT EXISTS voice_catalog (
  id VARCHAR(60) PRIMARY KEY,
  display_name VARCHAR(100),
  language VARCHAR(10),
  gender VARCHAR(10),                      -- male | female | child | neutral
  age_range VARCHAR(20),                   -- adult | senior | child | teen
  style VARCHAR(40),                       -- neutral | warm | professional | dramatic
  sample_url TEXT,
  is_premium BOOLEAN DEFAULT FALSE,
  cost_per_char_credits NUMERIC(10,8) DEFAULT 0.00001
);

INSERT INTO voice_catalog (id, display_name, language, gender, age_range, style, is_premium, cost_per_char_credits) VALUES
  ('vn-female-1', 'Linh — Nu mien Bac (warm)',     'vi', 'female', 'adult',  'warm',         FALSE, 0.00001),
  ('vn-female-2', 'Hue — Nu mien Trung (gentle)',  'vi', 'female', 'adult',  'gentle',       FALSE, 0.00001),
  ('vn-female-3', 'Nga — Nu mien Nam (lively)',    'vi', 'female', 'adult',  'lively',       FALSE, 0.00001),
  ('vn-male-1',   'Tuan — Nam mien Bac (clear)',   'vi', 'male',   'adult',  'professional', FALSE, 0.00001),
  ('vn-male-2',   'Phuc — Nam mien Trung (deep)',  'vi', 'male',   'adult',  'deep',         FALSE, 0.00001),
  ('vn-male-3',   'Khoa — Nam mien Nam (friendly)','vi', 'male',   'adult',  'friendly',     FALSE, 0.00001),
  ('vn-child-1',  'Mi — Be gai (cheerful)',        'vi', 'child',  'child',  'cheerful',     TRUE,  0.00002),
  ('vn-news-1',   'Anchor — Nu phat thanh vien',   'vi', 'female', 'adult',  'broadcast',    TRUE,  0.00002),
  ('en-female-1', 'Sarah — Eng US (neutral)',      'en', 'female', 'adult',  'neutral',      FALSE, 0.00001),
  ('en-male-1',   'Mike — Eng US (clear)',         'en', 'male',   'adult',  'professional', FALSE, 0.00001)
ON CONFLICT (id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  cost_per_char_credits = EXCLUDED.cost_per_char_credits;

COMMENT ON TABLE voice_stt_jobs IS 'Speech-to-text jobs: Whisper VN-tuned';
COMMENT ON TABLE voice_tts_jobs IS 'Text-to-speech jobs: XTTS-v3 VN';
COMMENT ON TABLE voice_catalog IS 'Available TTS voices: 6 VN + 2 EN + premium voices';
