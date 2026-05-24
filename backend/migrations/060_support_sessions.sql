-- Migration 060 — Zeni Cloud Support Center
-- Pattern ClawWits: chat workspace ↔ CTO + AI agent auto-reply task simple
-- + human-in-the-loop approval cho task phức tạp

CREATE TABLE IF NOT EXISTS support_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  customer_user_id UUID,                    -- Owner/Admin của workspace
  title VARCHAR(255) NOT NULL,
  status VARCHAR(20) DEFAULT 'open',        -- open | pending_approval | resolved | closed
  priority VARCHAR(20) DEFAULT 'normal',    -- low | normal | high | urgent
  category VARCHAR(40),                     -- deploy | billing | api | bug | feature_request
  assigned_to UUID,                         -- chairman or support staff
  message_count INT DEFAULT 0,
  last_message_at TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_support_sessions_ws ON support_sessions(workspace_id, status, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_sessions_chair ON support_sessions(status, last_message_at DESC) WHERE status IN ('open','pending_approval');

CREATE TABLE IF NOT EXISTS support_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES support_sessions(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  -- Sender
  sender_type VARCHAR(20) NOT NULL,         -- customer | agent | chairman | system
  sender_user_id UUID,
  sender_name VARCHAR(120),
  -- Content
  content TEXT NOT NULL,
  content_format VARCHAR(20) DEFAULT 'markdown',  -- markdown | code | json
  -- AI agent metadata
  ai_model VARCHAR(80),                     -- claude-sonnet-4-7 / gemini-2-pro / etc.
  ai_tokens_used INT,
  ai_confidence FLOAT,                      -- 0-1 (low confidence → escalate to chairman)
  -- Action proposed (chairman approve trước khi execute)
  proposed_action JSONB,                    -- {tool: "deploy_project", args: {...}, requires_approval: true}
  action_status VARCHAR(20),                -- proposed | approved | rejected | executed | failed
  action_executed_by UUID,
  action_executed_at TIMESTAMPTZ,
  action_result JSONB,
  -- Attachments
  attachments JSONB DEFAULT '[]'::jsonb,    -- [{type, name, url}]
  -- Audit
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_support_msgs_session ON support_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_support_msgs_pending ON support_messages(action_status) WHERE action_status = 'proposed';

-- Knowledge base entries (FAQ — agent reuse trả lời)
CREATE TABLE IF NOT EXISTS support_kb (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  category VARCHAR(40) NOT NULL,
  question TEXT NOT NULL,
  answer_markdown TEXT NOT NULL,
  keywords JSONB DEFAULT '[]'::jsonb,
  embedding VECTOR(768),                    -- pgvector cho semantic search
  use_count INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed FAQ chuẩn (đồng bộ với docs)
INSERT INTO support_kb (category, question, answer_markdown, keywords) VALUES
  ('deploy', 'Image not found khi deploy project',
   'Cloud Run reject vì image chưa được build/push. 3 cách fix:\n1. Upload ZIP source: POST /upload/source — Zeni tự build\n2. Build Farm: POST /build-farm/jobs cho Tauri/Rust\n3. Push lên Docker Hub `docker.io/yourname/app:v1` rồi add prefix vào workspace whitelist',
   '["image","not found","422","registry"]'::jsonb),
  ('deploy', 'missing bearer token 401',
   'Token JWT trong browser hết hạn (TTL 1h). Logout → Login lại → frontend tự refresh token. Nếu API call qua CLI/CI: dùng PAT scope=full thay vì JWT.',
   '["bearer","token","401","auth","expired"]'::jsonb),
  ('deploy', 'Container failed PORT',
   'App phải listen $PORT env (default 8080). Sửa code: `app.listen(process.env.PORT || 8080)`. Hoặc khi tạo project, set field "port" đúng app của bạn.',
   '["port","container","startup","probe"]'::jsonb),
  ('deploy', 'ghcr.io không pull được',
   'Cloud Run KHÔNG pull image trực tiếp từ ghcr.io. Dùng:\n• docker.io/yourname/ (Docker Hub)\n• us-central1-docker.pkg.dev/zeni-cloud-core/{ws}/ (Zeni AR — chairman cấp SA key)',
   '["ghcr","github registry","unsupported"]'::jsonb),
  ('billing', 'Topup wallet VND',
   'POST /api/v1/billing/wallet/topup — trả VietQR EMV code (NAPAS) → khách scan qua app banking VN (TPB/VCB/MBB/BIDV/ACB/TCB) → tự động credit wallet sau ~1 phút.',
   '["topup","wallet","vnd","vietqr","payment"]'::jsonb),
  ('api', 'Tạo PAT (Personal Access Token)',
   'POST /api/v1/api-tokens?ws={workspace} với body {name, scopes}. Scopes: ai/data/web3/automation/full/deploy/read. Token hiển thị MỘT LẦN — copy ngay.',
   '["pat","api token","cli","secret"]'::jsonb)
ON CONFLICT DO NOTHING;

COMMENT ON TABLE support_sessions IS 'Zeni Cloud Support — chat workspace ↔ chairman/agent (Intercom + Claude Code style)';
COMMENT ON TABLE support_messages IS 'Tin nhắn trong support session, có proposed_action cho human-in-the-loop';
COMMENT ON TABLE support_kb IS 'Knowledge base FAQ — AI agent retrieve trước khi gọi LLM (giảm cost + tăng accuracy)';
