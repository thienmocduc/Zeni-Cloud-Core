-- ═══════════════════════════════════════════════════════════
-- ZENI CLOUD CORE · INITIAL SCHEMA + SEED
-- Runs on first postgres boot via docker-entrypoint-initdb.d
-- ═══════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── Workspaces (8 entities) ──────────────────────────────
CREATE TABLE workspaces (
  id            VARCHAR(32) PRIMARY KEY,
  code          VARCHAR(8) UNIQUE NOT NULL,
  name          VARCHAR(128) NOT NULL,
  tagline       TEXT,
  color         VARCHAR(32),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Users (Zeni ID) ──────────────────────────────────────
CREATE TABLE users (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  email          VARCHAR(255) UNIQUE NOT NULL,
  password_hash  VARCHAR(255) NOT NULL,
  name           VARCHAR(128) NOT NULL,
  role           VARCHAR(32) NOT NULL DEFAULT 'Developer',   -- Owner|Admin|Developer|Viewer
  avatar         VARCHAR(255),
  mfa_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
  last_login     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  disabled       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_users_email ON users(email);

-- ─── User × Workspace access ──────────────────────────────
CREATE TABLE user_workspaces (
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  role           VARCHAR(32) NOT NULL DEFAULT 'Developer',
  PRIMARY KEY (user_id, workspace_id)
);

-- ─── L1 · Projects (Compute services) ─────────────────────
CREATE TABLE projects (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name           VARCHAR(128) NOT NULL,
  type           VARCHAR(32) NOT NULL,               -- web|api|worker|agent
  runtime        VARCHAR(32) NOT NULL,
  size           VARCHAR(8) NOT NULL DEFAULT 's',
  region         VARCHAR(32) NOT NULL DEFAULT 'asia-southeast1',
  status         VARCHAR(32) NOT NULL DEFAULT 'pending',
  instances      INT NOT NULL DEFAULT 1,
  cpu            VARCHAR(16) DEFAULT '0.5 vCPU',
  memory         VARCHAR(16) DEFAULT '1GB',
  domain         VARCHAR(255),
  last_deploy    TIMESTAMPTZ,
  version        VARCHAR(16) DEFAULT 'v1',
  git_ref        VARCHAR(64) DEFAULT 'main',
  created_by     UUID REFERENCES users(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (workspace_id, name)
);

CREATE INDEX idx_projects_ws ON projects(workspace_id);

-- ─── L2 · Databases (SQL schemas registered) ──────────────
CREATE TABLE databases (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name           VARCHAR(128) NOT NULL,
  kind           VARCHAR(16) NOT NULL,               -- sql|vector|object
  description    TEXT,
  row_count      BIGINT DEFAULT 0,
  dim            INT,                                -- for vector
  size_bytes     BIGINT DEFAULT 0,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_databases_ws ON databases(workspace_id);

-- ─── L3 · Agents (AI runtime) ─────────────────────────────
CREATE TABLE agents (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name           VARCHAR(128) NOT NULL,
  role           VARCHAR(128),
  model          VARCHAR(64) NOT NULL,
  system_prompt  TEXT,
  calls          INT NOT NULL DEFAULT 0,
  cost_usd       NUMERIC(12, 6) NOT NULL DEFAULT 0,
  status         VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agents_ws ON agents(workspace_id);

-- ─── L4 · Connectors ──────────────────────────────────────
CREATE TABLE connectors (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  type           VARCHAR(64) NOT NULL,
  status         VARCHAR(16) NOT NULL DEFAULT 'disconnected',
  events_7d      INT NOT NULL DEFAULT 0,
  config         JSONB DEFAULT '{}',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── L5 · Secrets (encrypted vault) ───────────────────────
CREATE TABLE secrets (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name           VARCHAR(128) NOT NULL,
  env            VARCHAR(16) NOT NULL DEFAULT 'prod',
  value_encrypted BYTEA NOT NULL,
  rotations      INT NOT NULL DEFAULT 0,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (workspace_id, name, env)
);

-- ─── L6 · Smart contracts ─────────────────────────────────
CREATE TABLE contracts (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name           VARCHAR(128) NOT NULL,
  description    TEXT,
  chain          VARCHAR(32) NOT NULL,
  address        VARCHAR(64),
  status         VARCHAR(16) NOT NULL DEFAULT 'draft',   -- draft|audited|deployed
  tx_hash        VARCHAR(80),
  deployed_at    TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Members invites ──────────────────────────────────────
CREATE TABLE member_invites (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  email          VARCHAR(255) NOT NULL,
  role           VARCHAR(32) NOT NULL DEFAULT 'Developer',
  token          VARCHAR(64) UNIQUE NOT NULL,
  status         VARCHAR(16) NOT NULL DEFAULT 'pending',
  invited_by     UUID REFERENCES users(id),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at     TIMESTAMPTZ
);

-- ─── Audit log (immutable) ────────────────────────────────
CREATE TABLE audit_log (
  id             BIGSERIAL PRIMARY KEY,
  ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor          VARCHAR(255),
  workspace_id   VARCHAR(32),
  action         VARCHAR(64) NOT NULL,
  target         VARCHAR(255),
  severity       VARCHAR(8) NOT NULL DEFAULT 'info',   -- info|ok|warn|err
  metadata       JSONB DEFAULT '{}'
);

CREATE INDEX idx_audit_ts ON audit_log(ts DESC);
CREATE INDEX idx_audit_ws ON audit_log(workspace_id);

-- ─── Billing meters ───────────────────────────────────────
CREATE TABLE billing_events (
  id             BIGSERIAL PRIMARY KEY,
  ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  workspace_id   VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  layer          VARCHAR(4) NOT NULL,      -- L1..L6
  action         VARCHAR(64) NOT NULL,
  cost_usd       NUMERIC(14, 8) NOT NULL
);

CREATE INDEX idx_billing_ws_ts ON billing_events(workspace_id, ts DESC);

-- ─── Refresh tokens ───────────────────────────────────────
CREATE TABLE refresh_tokens (
  jti            VARCHAR(64) PRIMARY KEY,
  user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at     TIMESTAMPTZ NOT NULL,
  revoked        BOOLEAN NOT NULL DEFAULT FALSE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════
-- SEED · 8 WORKSPACES (Zeni ecosystem)
-- ═══════════════════════════════════════════════════════════

INSERT INTO workspaces (id, code, name, tagline, color) VALUES
  ('holdings', 'HLD', 'Zeni Holdings',       'Corporate HQ',                      'var(--e-holdings)'),
  ('anima',    'ANI', 'ANIMA Care Global',   'Health & Wellness',                 'var(--e-anima)'),
  ('zeniipo',  'ZIP', 'Zeniipo',             'IPO Journey SaaS',                  'var(--e-zeniipo)'),
  ('digital',  'ZDI', 'Zeni Digital',        'Horizontal SaaS',                   'var(--e-digital)'),
  ('wellkoc',  'WKC', 'WellKOC',             'Social Commerce',                   'var(--e-wellkoc)'),
  ('nexbuild', 'NXB', 'NexBuild',            'ConstrucTech',                      'var(--e-nexbuild)'),
  ('bthome',   'BTH', 'bthome',              'Design & Build',                    'var(--e-bthome)'),
  ('capital',  'ZCP', 'Zeni Capital',        'Digital Finance',                   'var(--e-capital)');

-- Sample databases (L2)
INSERT INTO databases (workspace_id, name, kind, description, row_count, dim, size_bytes) VALUES
  ('anima',    'shared_identity', 'sql',    'users, orgs, sessions — SSO xuyên workspace', 1200000, NULL, 204800000),
  ('anima',    'anima_main',      'sql',    'customers, bookings, stations, ANIMA 119 orders', 412000, NULL, 102400000),
  ('zeniipo',  'zeniipo_main',    'sql',    'ipo_journeys, companies, filings, agent_runs', 89000, NULL, 51200000),
  ('digital',  'digital_main',    'sql',    'tenants, workspaces, invoices, activities', 398000, NULL, 82000000),
  ('wellkoc',  'wellkoc_main',    'sql',    'kocs, campaigns, commissions, live_sessions', 244000, NULL, 65000000),
  ('anima',    'anima_kb',        'vector', 'Knowledge base sức khoẻ', 84000, 1536, 258000000),
  ('zeniipo',  'zeniipo_docs',    'vector', 'Hồ sơ IPO đã scan', 12000, 1536, 36000000),
  ('digital',  'digital_faq',     'vector', 'FAQ xuyên sản phẩm', 3400, 768, 5120000),
  ('holdings', 'zeni-uploads-prod', 'object', 'User uploads bucket', 18244, NULL, 2147483648),
  ('holdings', 'zeni-assets-prod',  'object', 'Static assets bucket', 3108, NULL, 12884901888);

-- Sample agents (L3)
INSERT INTO agents (workspace_id, name, role, model, system_prompt, calls, cost_usd, status) VALUES
  ('anima',   'ANIMA Care Advisor', 'Tư vấn sức khoẻ 24/7', 'claude-sonnet-4-6', 'Bạn là tư vấn viên sức khoẻ ANIMA Care. Hướng dẫn khách hàng chọn liệu trình phù hợp.', 3420, 18.50, 'active'),
  ('zeniipo', 'IPO Journey Advisor', 'Hướng dẫn DN chuẩn bị IPO', 'claude-opus-4-7',   'Bạn là chuyên gia IPO, hỗ trợ doanh nghiệp VN chuẩn bị niêm yết.', 892, 42.80, 'active'),
  ('wellkoc', 'KOC Content Writer',  'Viết caption livestream + video',   'claude-sonnet-4-6', 'Bạn viết caption TikTok, hook 3 giây, call-to-action mạnh.', 5210, 11.20, 'active'),
  ('digital', 'Support Bot',         'Customer support tier-1', 'claude-sonnet-4-6', 'Bạn trả lời khách hàng bằng giọng thân thiện, chính xác.', 8120, 14.40, 'active'),
  ('nexbuild','Interior Render',     'Sinh ảnh nội thất 3D',    'sd-lora-interior',  'Generate interior renders for Vietnamese villa projects.', 142, 5.68, 'paused');

-- Sample connectors (L4)
INSERT INTO connectors (workspace_id, type, status, events_7d) VALUES
  ('anima',   'Zalo OA',       'connected', 4280),
  ('anima',   'VNPay',         'connected', 1248),
  ('wellkoc', 'TikTok Shop',   'connected', 8420),
  ('wellkoc', 'Shopee',        'connected', 2180),
  ('digital', 'Slack',         'connected', 421),
  ('digital', 'SendGrid',      'connected', 1820),
  ('zeniipo', 'Google Sheets', 'connected', 82);

-- Sample contracts (L6)
INSERT INTO contracts (workspace_id, name, description, chain, address, status, deployed_at) VALUES
  ('wellkoc', 'ZeniReward ERC-20',     'Token thưởng KOC + loyalty',          'polygon',    '0x4a2b1c3d4e5f6789abcdef0123456789abcdef01', 'deployed', NOW() - INTERVAL '30 days'),
  ('anima',   'ANIMA NFT Cert',        'Chứng nhận liệu trình ANIMA Care',    'polygon',    '0x7f3c4d5e6f789012abcdef3456789012abcdef67', 'deployed', NOW() - INTERVAL '14 days'),
  ('holdings','Zeni Land Deed',        'Sổ đỏ on-chain bất động sản',         'zeni_chain', NULL,                                         'audited',  NULL),
  ('holdings','Vesting Schedule',      'Phân phối token nhân viên',           'polygon',    NULL,                                         'audited',  NULL),
  ('holdings','DAO Governance',        'Biểu quyết Founding Members',         'zeni_chain', NULL,                                         'draft',    NULL);
