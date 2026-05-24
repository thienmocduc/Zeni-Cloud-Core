-- Migration 062 — Zeni Coder Swarm (CodeWits-equivalent)
-- 6-vai Council Pattern: Architect/Planner/Coder/Reviewer/Security/QA
-- Route qua Zeni Router (DeepSeek 80%/Claude 15%/GPT 3%/Gemini 2%)
-- Memory pgvector cho RAG retrieval
-- Live workstream pattern WebSocket

-- ═══════════════════════════════════════════════════════════════
-- 1. Coder runs — mỗi requirement chairman = 1 run
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cto_coder_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID REFERENCES support_sessions(id) ON DELETE SET NULL,
  requested_by UUID NOT NULL,                -- chairman/CTO user_id
  target_workspace VARCHAR(64),              -- workspace KHÁCH (nếu deploy cho khách)
  target_project_id VARCHAR(64),             -- project cụ thể
  -- Input
  requirement TEXT NOT NULL,                 -- yêu cầu chairman gõ
  context JSONB DEFAULT '{}'::jsonb,         -- previous run refs, file snapshots
  -- Council output
  architect_design JSONB,                    -- {stack, components, integrations}
  planner_steps JSONB,                       -- [{step, tool, args, depends_on}]
  council_consensus VARCHAR(20),             -- pending|approved|veto|conflict
  council_votes JSONB DEFAULT '[]'::jsonb,   -- [{agent, vote: yes|no|abstain, reason}]
  -- Execution
  status VARCHAR(20) DEFAULT 'planning',     -- planning|approved|executing|completed|failed|aborted
  current_step_idx INT DEFAULT 0,
  total_steps INT,
  -- Cost tracking (Zeni Router)
  total_cost_usd FLOAT DEFAULT 0,
  total_input_tokens INT DEFAULT 0,
  total_output_tokens INT DEFAULT 0,
  -- Timing
  created_at TIMESTAMPTZ DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  duration_ms INT,
  -- Result
  final_result JSONB,
  error_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_coder_runs_session ON cto_coder_runs(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_coder_runs_status ON cto_coder_runs(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_coder_runs_workspace ON cto_coder_runs(target_workspace, created_at DESC) WHERE target_workspace IS NOT NULL;

-- ═══════════════════════════════════════════════════════════════
-- 2. Council votes — chi tiết từng vai's reasoning
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cto_council_votes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES cto_coder_runs(id) ON DELETE CASCADE,
  agent_role VARCHAR(20) NOT NULL,           -- architect|planner|coder|reviewer|security|qa
  agent_model VARCHAR(80) NOT NULL,          -- claude-sonnet-4-6 / deepseek-v4 / etc.
  -- Output
  vote VARCHAR(10),                          -- yes|no|abstain|veto
  reasoning TEXT,
  output_json JSONB,                         -- structured output (plan/scan/etc.)
  -- Cost
  input_tokens INT DEFAULT 0,
  output_tokens INT DEFAULT 0,
  cost_usd FLOAT DEFAULT 0,
  latency_ms INT,
  -- Routing decision
  router_decision JSONB,                     -- {model_chosen, reason, fallback_chain}
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_council_votes_run ON cto_council_votes(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_council_votes_agent ON cto_council_votes(agent_role, created_at DESC);

-- ═══════════════════════════════════════════════════════════════
-- 3. Run steps — checkpoint từng bước trong plan
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cto_run_steps (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES cto_coder_runs(id) ON DELETE CASCADE,
  step_idx INT NOT NULL,
  -- Step definition (from Planner)
  tool_name VARCHAR(80) NOT NULL,
  tool_args JSONB DEFAULT '{}'::jsonb,
  depends_on JSONB DEFAULT '[]'::jsonb,      -- [step_idx, ...]
  description TEXT,
  -- Execution state
  status VARCHAR(20) DEFAULT 'pending',      -- pending|approved|executing|completed|failed|skipped
  retry_count INT DEFAULT 0,
  max_retries INT DEFAULT 3,
  -- Approval (chairman duyệt nếu medium+)
  requires_approval BOOLEAN DEFAULT TRUE,
  approved_by UUID,
  approved_at TIMESTAMPTZ,
  rejected_reason TEXT,
  -- Execution result
  executed_at TIMESTAMPTZ,
  duration_ms INT,
  result JSONB,
  error_detail TEXT,
  -- Reviewer + QA verdict
  reviewer_verdict VARCHAR(20),              -- pass|fail|warn
  reviewer_notes TEXT,
  qa_verdict VARCHAR(20),
  qa_notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_run_steps_run ON cto_run_steps(run_id, step_idx);
CREATE INDEX IF NOT EXISTS idx_run_steps_pending ON cto_run_steps(status, run_id) WHERE status IN ('pending', 'approved');

-- ═══════════════════════════════════════════════════════════════
-- 4. Agent memory — pgvector RAG
-- ═══════════════════════════════════════════════════════════════
-- Cho agent recall: "lần trước task X đã làm gì, bug Y fix sao"
CREATE TABLE IF NOT EXISTS cto_agent_memory (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope VARCHAR(40) NOT NULL,                -- workspace_id | 'global'
  memory_type VARCHAR(20) NOT NULL,          -- plan|observation|error|knowledge|skill
  title VARCHAR(255),
  content TEXT NOT NULL,                     -- markdown
  metadata JSONB DEFAULT '{}'::jsonb,        -- {run_id, step_idx, tags}
  embedding VECTOR(768),                     -- pgvector cho semantic retrieve
  use_count INT DEFAULT 0,
  last_used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_scope ON cto_agent_memory(scope, memory_type);
-- Vector index (HNSW) cho semantic search nhanh
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
    CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
      ON cto_agent_memory USING hnsw (embedding vector_cosine_ops);
  END IF;
EXCEPTION WHEN OTHERS THEN
  -- pgvector chưa enable hoặc HNSW không support → bỏ qua, dùng L2 sequential scan
  NULL;
END $$;

-- ═══════════════════════════════════════════════════════════════
-- 5. Skill registry — composite tools cao cấp
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cto_skill_registry (
  skill_name VARCHAR(80) PRIMARY KEY,
  display_name VARCHAR(120) NOT NULL,
  description TEXT NOT NULL,
  -- Composite definition
  tool_sequence JSONB NOT NULL,              -- [{tool, args_template, on_fail: 'abort'|'retry'|'skip'}]
  required_args JSONB DEFAULT '[]'::jsonb,   -- ['workspace', 'template']
  -- Risk + access
  risk_level VARCHAR(20) DEFAULT 'medium',
  allowed_roles JSONB DEFAULT '["Owner"]'::jsonb,
  -- Stats
  use_count INT DEFAULT 0,
  avg_duration_ms INT,
  success_rate FLOAT,
  -- Meta
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed 5 core skills (composite of low-level tools in cto_tool_policy)
INSERT INTO cto_skill_registry (skill_name, display_name, description, tool_sequence, required_args, risk_level) VALUES
  ('template_deploy',
   'Template Deploy (1-click)',
   'Tạo project Cloud Run từ template pre-built (Next.js/FastAPI/Tauri/Vite/etc.) → build → deploy --no-traffic',
   '[
     {"tool": "create_project", "args_template": {"workspace": "$workspace", "name": "$name", "image": "$template_image"}, "on_fail": "abort"},
     {"tool": "trigger_build", "args_template": {"workspace": "$workspace", "project_id": "$project_id"}, "on_fail": "retry"},
     {"tool": "deploy_canary", "args_template": {"workspace": "$workspace", "project_id": "$project_id"}, "on_fail": "abort"}
   ]'::jsonb,
   '["workspace", "name", "template"]'::jsonb,
   'medium'),
  ('read_project_repo',
   'Read Project Repo (RAG)',
   'Đọc source code khách qua source_upload hoặc GitHub clone → load file snapshots → ready cho agent reasoning',
   '[
     {"tool": "list_files", "args_template": {"workspace": "$workspace", "project_id": "$project_id"}, "on_fail": "abort"}
   ]'::jsonb,
   '["workspace", "project_id"]'::jsonb,
   'safe'),
  ('propose_code_patch',
   'Propose Code Patch (diff)',
   'Agent đọc snapshot → generate unified diff → chairman duyệt → apply vào snapshot mới',
   '[
     {"tool": "get_file", "args_template": {"file_id": "$file_id"}, "on_fail": "abort"},
     {"tool": "send_message", "args_template": {"session_id": "$session_id", "content": "$diff"}, "on_fail": "skip"}
   ]'::jsonb,
   '["session_id", "file_id"]'::jsonb,
   'medium'),
  ('run_canary_with_smoke',
   'Run Canary + Smoke Test',
   'Deploy --no-traffic → curl /health 5 lần → check error log → return verdict pass/fail',
   '[
     {"tool": "deploy_canary", "args_template": {"workspace": "$workspace", "project_id": "$project_id"}, "on_fail": "abort"},
     {"tool": "get_project_logs", "args_template": {"project_id": "$project_id", "minutes": 2}, "on_fail": "skip"}
   ]'::jsonb,
   '["workspace", "project_id"]'::jsonb,
   'high'),
  ('staged_promote',
   'Staged Promote 10/50/100 (auto-rollback)',
   'Promote 10% → smoke 60s → 50% → smoke 60s → 100%. Auto-rollback nếu error rate > 1%',
   '[
     {"tool": "promote_traffic", "args_template": {"project_id": "$project_id", "percent": 10}, "on_fail": "abort"},
     {"tool": "get_project_logs", "args_template": {"project_id": "$project_id", "minutes": 1}, "on_fail": "skip"},
     {"tool": "promote_traffic", "args_template": {"project_id": "$project_id", "percent": 50}, "on_fail": "abort"},
     {"tool": "get_project_logs", "args_template": {"project_id": "$project_id", "minutes": 1}, "on_fail": "skip"},
     {"tool": "promote_traffic", "args_template": {"project_id": "$project_id", "percent": 100}, "on_fail": "abort"}
   ]'::jsonb,
   '["project_id"]'::jsonb,
   'high')
ON CONFLICT (skill_name) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  description = EXCLUDED.description,
  tool_sequence = EXCLUDED.tool_sequence,
  required_args = EXCLUDED.required_args,
  risk_level = EXCLUDED.risk_level,
  updated_at = NOW();

-- Add propose_edit + apply_edit tools to cto_tool_policy
INSERT INTO cto_tool_policy (tool_name, risk_level, description, endpoint_path) VALUES
  ('list_files',           'safe',  'List file snapshots in coder session', 'GET /cto/files'),
  ('get_file',             'safe',  'Get full content of file snapshot', 'GET /cto/files/{id}'),
  ('propose_edit',         'medium','Agent propose code diff cho 1 file', 'INTERNAL'),
  ('apply_edit',           'medium','Apply approved diff vào snapshot mới + optional GitHub PR', 'INTERNAL'),
  ('search_codebase',      'safe',  'Semantic search across snapshots (pgvector)', 'INTERNAL'),
  ('recall_memory',        'safe',  'Retrieve agent memory (pgvector RAG)', 'INTERNAL'),
  ('save_memory',          'low',   'Save observation/learning vào memory', 'INTERNAL')
ON CONFLICT (tool_name) DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- 6. Comments
-- ═══════════════════════════════════════════════════════════════
COMMENT ON TABLE cto_coder_runs IS 'Zeni Coder Swarm — mỗi run = 1 yêu cầu chairman';
COMMENT ON TABLE cto_council_votes IS '6-vai voting log với cost tracking qua Zeni Router';
COMMENT ON TABLE cto_run_steps IS 'Checkpoint từng bước trong plan + reviewer/qa verdict';
COMMENT ON TABLE cto_agent_memory IS 'Pgvector RAG memory — agent recall observation/learning';
COMMENT ON TABLE cto_skill_registry IS 'Composite skills cao cấp — bundle nhiều low-level tools';
