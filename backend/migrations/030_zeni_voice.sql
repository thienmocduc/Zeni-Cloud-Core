-- ============================================================================
-- Migration 030 — Zeni Voice (Voice/SMS module, replaces Stringee voice)
--
-- Replaces and extends Stringee's voice + SMS APIs with a native, multi-provider
-- contact center stack:
--   * Phone numbers (rented per workspace; Twilio + Vietnamese carriers)
--   * Inbound + outbound calls with recording, transcript, sentiment
--   * Visual IVR flows (DAG nodes: gather, say, dial, hangup, ...)
--   * Call queues + agents (round-robin / least-busy / priority)
--   * Voicemails with transcription + read tracking
--   * TTS / STT usage metering (Google Cloud + Vietnamese voices)
--
-- Strategy: BUILD native, do NOT integrate Stringee voice. The existing SMS
-- abstraction (Stream A4, app/services/sms.py) is kept as-is — Zeni Voice is
-- additive and lives behind /api/v1/voice/*.
-- ============================================================================

-- ─── 1. Phone numbers (rented) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_numbers (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    phone_number VARCHAR(20) NOT NULL,         -- E.164 +84xxx
    provider VARCHAR(20) DEFAULT 'twilio',     -- 'twilio','viettel','fpt','vnpt'
    capabilities TEXT[] DEFAULT ARRAY['voice','sms'],
    monthly_cost_usd NUMERIC(8,2),
    status VARCHAR(20) DEFAULT 'active',       -- 'active','suspended','released'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (phone_number)
);
CREATE INDEX IF NOT EXISTS idx_voice_numbers_ws ON voice_numbers(workspace_id, status);

-- ─── 2. Calls (inbound + outbound) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_calls (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    call_sid VARCHAR(80) UNIQUE NOT NULL,       -- Provider call ID
    direction VARCHAR(20) NOT NULL,             -- 'inbound','outbound'
    from_number VARCHAR(20),
    to_number VARCHAR(20),
    duration_seconds INT,
    status VARCHAR(30),                          -- 'queued','ringing','in-progress','completed','no-answer','busy','failed'
    recording_url TEXT,
    transcript TEXT,
    sentiment_score NUMERIC(3,2),
    cost_usd NUMERIC(8,4) DEFAULT 0,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    metadata JSONB
);
CREATE INDEX IF NOT EXISTS idx_voice_calls_ws ON voice_calls(workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_calls_dir ON voice_calls(workspace_id, direction, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_calls_status ON voice_calls(workspace_id, status);

-- ─── 3. IVR flows (visual flow definitions) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_ivr_flows (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    welcome_message TEXT,
    nodes JSONB NOT NULL,                        -- DAG of nodes: gather, say, dial, hangup
    associated_number_id BIGINT REFERENCES voice_numbers(id),
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_voice_ivr_ws ON voice_ivr_flows(workspace_id, is_active);

-- ─── 4. Call queues (contact center) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_queues (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    routing_strategy VARCHAR(20) DEFAULT 'round-robin', -- 'round-robin','least-busy','priority'
    max_wait_seconds INT DEFAULT 300,
    overflow_action VARCHAR(40),                 -- 'voicemail','callback','external'
    overflow_target TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_voice_queues_ws ON voice_queues(workspace_id);

-- ─── 5. Agents (call-center operators) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_agents (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_email TEXT NOT NULL,
    extension VARCHAR(10),
    skills TEXT[],
    status VARCHAR(20) DEFAULT 'offline',        -- 'online','busy','away','offline'
    queue_ids BIGINT[],
    last_active_at TIMESTAMPTZ,
    UNIQUE (workspace_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_voice_agents_status ON voice_agents(workspace_id, status);

-- ─── 6. Voicemails ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_voicemails (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    call_id BIGINT REFERENCES voice_calls(id) ON DELETE SET NULL,
    from_number VARCHAR(20),
    to_number VARCHAR(20),
    audio_url TEXT,
    transcript TEXT,
    duration_seconds INT,
    listened BOOLEAN DEFAULT FALSE,
    listened_by TEXT,
    listened_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_voice_voicemails_ws ON voice_voicemails(workspace_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_voicemails_unread ON voice_voicemails(workspace_id, listened);

-- ─── 7. TTS / STT usage (metering) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS voice_speech_usage (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    operation VARCHAR(20) NOT NULL,              -- 'tts','stt'
    text_length INT,
    audio_duration_seconds NUMERIC(10,3),
    voice VARCHAR(60),                            -- 'vi-VN-Standard-A' etc.
    cost_usd NUMERIC(8,6),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_voice_speech_ws ON voice_speech_usage(workspace_id, created_at DESC);
