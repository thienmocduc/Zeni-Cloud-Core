-- Migration 061 — Zeni Cloud CTO Console
-- Pattern: Claude Code-style 3-tab workspace cho CTO operations
-- (Chat Support · Provisioning · Auto Coder) + AI agent tool use
-- + Human-in-the-loop approval cho mọi destructive action.

-- ═══════════════════════════════════════════════════════════════
-- 1. CTO sessions — extend support_sessions với type discriminator
-- ═══════════════════════════════════════════════════════════════
-- Thay vì tạo bảng mới, thêm cột session_type vào support_sessions
-- (idempotent — IF NOT EXISTS).
ALTER TABLE support_sessions
  ADD COLUMN IF NOT EXISTS session_type VARCHAR(20) DEFAULT 'support';
  -- support | provisioning | coder

CREATE INDEX IF NOT EXISTS idx_support_sessions_type
  ON support_sessions(session_type, status, last_message_at DESC);

ALTER TABLE support_sessions
  ADD COLUMN IF NOT EXISTS target_workspace VARCHAR(64);
  -- Cho coder/provisioning sessions: workspace KHÁCH (CTO support cho ai)
ALTER TABLE support_sessions
  ADD COLUMN IF NOT EXISTS target_project_id VARCHAR(64);
  -- Optional: project cụ thể trong workspace khách

-- ═══════════════════════════════════════════════════════════════
-- 2. Agent tool calls — log mọi tool agent đề xuất + execute
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cto_agent_tool_calls (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES support_sessions(id) ON DELETE CASCADE,
  message_id UUID,                        -- support_messages.id (nếu có)
  -- Tool definition
  tool_name VARCHAR(80) NOT NULL,         -- create_pat / add_whitelist / deploy_canary / etc.
  tool_args JSONB NOT NULL,
  -- Approval workflow
  status VARCHAR(20) DEFAULT 'proposed',  -- proposed | approved | rejected | executing | executed | failed
  proposed_at TIMESTAMPTZ DEFAULT NOW(),
  proposed_by_agent VARCHAR(80),          -- claude-haiku-4-5 / gemini-2-pro / chairman_manual
  -- Approval & execution
  approved_by UUID,                       -- user_id (chairman/owner)
  approved_at TIMESTAMPTZ,
  rejected_reason TEXT,
  executed_at TIMESTAMPTZ,
  execution_result JSONB,
  execution_duration_ms INT,
  error_detail TEXT,
  -- Target workspace (cho audit cross-tenant)
  target_workspace VARCHAR(64),
  target_resource VARCHAR(255),           -- vd: "project:cafe-prod" hoặc "pat:tk_abc"
  -- Risk class — quyết định mặc định auto-approve hay phải duyệt
  risk_level VARCHAR(20) DEFAULT 'medium' -- safe | low | medium | high | destructive
);
CREATE INDEX IF NOT EXISTS idx_cto_tool_calls_session
  ON cto_agent_tool_calls(session_id, proposed_at);
CREATE INDEX IF NOT EXISTS idx_cto_tool_calls_pending
  ON cto_agent_tool_calls(status, proposed_at) WHERE status = 'proposed';
CREATE INDEX IF NOT EXISTS idx_cto_tool_calls_workspace
  ON cto_agent_tool_calls(target_workspace, executed_at DESC) WHERE executed_at IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════
-- 3. CTO project files — snapshot file content cho coder sessions
-- ═══════════════════════════════════════════════════════════════
-- Khi CTO load project khách vào Auto Coder tab, file content lưu
-- tạm ở đây để agent đọc + propose edit. Không lưu permanent.
CREATE TABLE IF NOT EXISTS cto_project_file_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES support_sessions(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  project_id VARCHAR(64),
  file_path TEXT NOT NULL,                -- vd: "src/app/page.tsx"
  content TEXT,                           -- raw content (max 200KB per file)
  content_size INT,
  language VARCHAR(40),                   -- typescript / python / etc.
  fetched_from VARCHAR(40),               -- source_upload | github | manual_paste
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'
);
CREATE INDEX IF NOT EXISTS idx_cto_files_session
  ON cto_project_file_snapshots(session_id, file_path);

-- ═══════════════════════════════════════════════════════════════
-- 4. CTO capability policy — định nghĩa risk_level cho mỗi tool
-- ═══════════════════════════════════════════════════════════════
-- Pattern ClawWits capability_policy.yaml — mỗi tool có risk class.
-- Tool safe/low → auto-execute không cần duyệt
-- Tool medium/high → propose → chairman duyệt
-- Tool destructive → propose + 2-step confirmation
CREATE TABLE IF NOT EXISTS cto_tool_policy (
  tool_name VARCHAR(80) PRIMARY KEY,
  risk_level VARCHAR(20) NOT NULL,        -- safe | low | medium | high | destructive
  description TEXT NOT NULL,
  endpoint_path VARCHAR(255),             -- backend endpoint mà tool này gọi
  allowed_roles JSONB DEFAULT '["Owner"]'::jsonb,
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO cto_tool_policy (tool_name, risk_level, description, endpoint_path) VALUES
  -- SAFE — read-only, auto-execute
  ('list_workspaces',     'safe',  'List workspaces khách', 'GET /workspaces'),
  ('list_projects',       'safe',  'List projects của 1 workspace', 'GET /projects'),
  ('get_project_logs',    'safe',  'Đọc logs project khách', 'GET /projects/{id}/logs'),
  ('list_kb_faq',         'safe',  'Search knowledge base FAQ', 'GET /support/kb'),
  -- LOW — non-destructive write, auto-execute
  ('send_message',        'low',   'Gửi message vào support session', 'POST /support/sessions/{id}/messages'),
  ('create_kb_entry',     'low',   'Thêm FAQ entry vào KB', 'POST /support/kb'),
  -- MEDIUM — needs approval (default behavior)
  ('create_pat',          'medium','Tạo Personal Access Token cho khách', 'POST /api-tokens'),
  ('add_image_whitelist', 'medium','Add registry prefix vào whitelist khách', 'POST /workspaces/{ws}/image-whitelist'),
  ('create_project',      'medium','Tạo project Cloud Run mới cho khách', 'POST /projects'),
  ('rotate_secret',       'medium','Rotate secret trong Vault khách', 'POST /identity/secrets/{id}/rotate'),
  -- HIGH — needs approval + warning
  ('deploy_canary',       'high',  'Deploy revision Cloud Run --no-traffic', 'POST /projects/{id}/deploy'),
  ('promote_traffic',     'high',  'Update Cloud Run traffic split (10/50/100)', 'POST /projects/{id}/traffic'),
  ('trigger_build',       'high',  'Trigger Build Farm job', 'POST /build-farm/jobs'),
  -- DESTRUCTIVE — needs 2-step confirmation
  ('delete_project',      'destructive','Xóa project Cloud Run của khách', 'DELETE /projects/{id}'),
  ('delete_secret',       'destructive','Xóa secret Vault khách', 'DELETE /identity/secrets/{id}'),
  ('rollback_deploy',     'destructive','Rollback project về previous revision', 'POST /projects/{id}/rollback')
ON CONFLICT (tool_name) DO UPDATE SET
  risk_level = EXCLUDED.risk_level,
  description = EXCLUDED.description,
  endpoint_path = EXCLUDED.endpoint_path;

-- ═══════════════════════════════════════════════════════════════
-- 5. Comments
-- ═══════════════════════════════════════════════════════════════
COMMENT ON TABLE cto_agent_tool_calls IS 'CTO Console — log mọi AI tool call propose + approve + execute (human-in-the-loop)';
COMMENT ON TABLE cto_project_file_snapshots IS 'CTO Console — file content cache 24h cho Auto Coder agent đọc + edit';
COMMENT ON TABLE cto_tool_policy IS 'CTO Console — risk classification cho mỗi tool (safe→destructive), enforce trong API';
