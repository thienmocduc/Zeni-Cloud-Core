-- ════════════════════════════════════════════════════════════════
-- Migration 076 — CTO Customer-facing Security tables
-- ════════════════════════════════════════════════════════════════
-- Tạo 2 bảng:
--   cto_security_violations  — log mỗi violation từ cto_input_filter
--   cto_workspace_locks      — active lock cho workspace (auto + manual)
--   customer_cto_sessions    — chat session của customer với CTO AI
--   customer_cto_messages    — messages trong session (đã filter)
--
-- IDEMPOTENT — dùng CREATE TABLE IF NOT EXISTS, ON CONFLICT DO NOTHING.
-- Approved: 2026-05-24 Chairman Thiên Mộc Đức.
-- ════════════════════════════════════════════════════════════════

-- ─── 1. Security violations log ────────────────────────────────
CREATE TABLE IF NOT EXISTS cto_security_violations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    VARCHAR(64) NOT NULL,
    user_id         UUID,
    session_id      VARCHAR(64),
    action          VARCHAR(20) NOT NULL,          -- block | sanitize
    severity        VARCHAR(20) NOT NULL,          -- info | warn | high | critical
    reasons         TEXT,
    matched_patterns TEXT,
    ip_address      VARCHAR(64),
    user_agent      VARCHAR(256),
    excerpt         VARCHAR(512),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cto_sec_viol_ws_time
    ON cto_security_violations (workspace_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cto_sec_viol_sev_time
    ON cto_security_violations (severity, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cto_sec_viol_ip
    ON cto_security_violations (ip_address)
    WHERE ip_address IS NOT NULL;


-- ─── 2. Workspace locks ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cto_workspace_locks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    VARCHAR(64) NOT NULL UNIQUE,
    ip_address      VARCHAR(64),
    locked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    unlock_at       TIMESTAMPTZ NOT NULL,
    severity        VARCHAR(20) NOT NULL DEFAULT 'warn',
    reason          VARCHAR(512)
);

CREATE INDEX IF NOT EXISTS idx_cto_locks_unlock
    ON cto_workspace_locks (unlock_at);

CREATE INDEX IF NOT EXISTS idx_cto_locks_ip
    ON cto_workspace_locks (ip_address)
    WHERE ip_address IS NOT NULL;


-- ─── 3. Customer CTO chat sessions ─────────────────────────────
CREATE TABLE IF NOT EXISTS customer_cto_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    VARCHAR(64) NOT NULL,
    user_id         UUID NOT NULL,
    title           VARCHAR(255) NOT NULL DEFAULT 'Hỗ trợ deploy',
    status          VARCHAR(20) NOT NULL DEFAULT 'open',
    message_count   INT NOT NULL DEFAULT 0,
    project_id      VARCHAR(64),
    model           VARCHAR(64) NOT NULL DEFAULT 'deepseek-chat',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_cust_cto_ses_ws_user
    ON customer_cto_sessions (workspace_id, user_id, created_at DESC);


-- ─── 4. Customer CTO messages ──────────────────────────────────
CREATE TABLE IF NOT EXISTS customer_cto_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES customer_cto_sessions(id) ON DELETE CASCADE,
    workspace_id    VARCHAR(64) NOT NULL,
    sender_type     VARCHAR(20) NOT NULL,          -- customer | cto_ai | system
    content         TEXT NOT NULL,
    content_filtered TEXT,                          -- after output_filter
    filter_warnings TEXT,                           -- jsonish list
    model           VARCHAR(64),
    input_tokens    INT DEFAULT 0,
    output_tokens   INT DEFAULT 0,
    cost_usd        NUMERIC(12,8) DEFAULT 0,
    latency_ms      INT DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cust_cto_msg_session
    ON customer_cto_messages (session_id, created_at);


-- ─── 5. Cleanup function (chạy bởi cron) ───────────────────────
-- Xóa violations > 7 ngày, locks đã expired > 1 ngày
CREATE OR REPLACE FUNCTION cleanup_cto_security() RETURNS INT AS $$
DECLARE
    deleted INT := 0;
    sub INT;
BEGIN
    DELETE FROM cto_security_violations
        WHERE created_at < NOW() - INTERVAL '7 days';
    GET DIAGNOSTICS sub = ROW_COUNT;
    deleted := deleted + sub;

    DELETE FROM cto_workspace_locks
        WHERE unlock_at < NOW() - INTERVAL '1 day';
    GET DIAGNOSTICS sub = ROW_COUNT;
    deleted := deleted + sub;

    RETURN deleted;
END;
$$ LANGUAGE plpgsql;
