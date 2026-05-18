-- Zeni Cloud Core · CTO Chat Assistant sessions (2026-05-19)
-- Each row = 1 conversation thread between khách and AI deploy orchestrator.

CREATE TABLE IF NOT EXISTS cto_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    VARCHAR(64) NOT NULL,
    user_email      VARCHAR(255),
    input_text      TEXT NOT NULL,                -- raw chat input from user
    input_type      VARCHAR(32),                  -- github | image | zip | description | unknown
    status          VARCHAR(32) NOT NULL DEFAULT 'analyzing',
                                                  -- analyzing | building | deploying | success | failed
    project_id      UUID,                         -- linked Project row if created
    project_url     TEXT,                         -- public URL after success
    messages        JSONB NOT NULL DEFAULT '[]'::jsonb,
                                                  -- [{ts, level, text}, …] streaming logs
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_cto_sessions_ws       ON cto_sessions(workspace_id);
CREATE INDEX IF NOT EXISTS idx_cto_sessions_status   ON cto_sessions(status);
CREATE INDEX IF NOT EXISTS idx_cto_sessions_created  ON cto_sessions(created_at DESC);

COMMENT ON TABLE  cto_sessions IS 'CTO Chat Assistant — orchestrates AI-driven deploy from chat input';
COMMENT ON COLUMN cto_sessions.input_type IS 'Detected from input_text: github URL, image URL, zip upload, or natural language';
COMMENT ON COLUMN cto_sessions.messages IS 'Append-only log: [{ts: ISO8601, level: info|warn|error, text: ...}]';
