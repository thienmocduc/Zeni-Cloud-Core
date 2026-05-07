-- ============================================================================
-- Migration 036 — Zeni CRM (HubSpot-like CRM for Zeni Cloud)
--
-- Purpose: Native CRM thay thế HubSpot cho SME Việt Nam.
--   * Contacts + Companies (CRM 360)
--   * Deals + Pipelines (Kanban sales pipeline)
--   * Activities (call/email/meeting/note/task) — log tương tác
--   * Tickets — support / customer service
--   * Sequences — email drip automation
--   * Lists (segments) — static + dynamic, dùng cho marketing/sequence
--
-- Tables:
--   crm_contacts             — Khách hàng tiềm năng / khách hàng
--   crm_companies            — Công ty / tổ chức
--   crm_deals                — Cơ hội bán hàng
--   crm_pipelines            — Pipeline + stages (JSONB)
--   crm_activities           — Lịch sử tương tác (call/email/meeting/note/task)
--   crm_tickets              — Yêu cầu hỗ trợ
--   crm_sequences            — Email drip (chuỗi email tự động)
--   crm_sequence_enrollments — Trạng thái enroll contact vào sequence
--   crm_lists                — Segments (static / dynamic)
--   crm_list_members         — Liên kết list-contact (cho static list)
-- ============================================================================

-- ─── 1. Companies (declared first vì contacts FK tới companies) ────────────
CREATE TABLE IF NOT EXISTS crm_companies (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(240) NOT NULL,
    domain          VARCHAR(240),
    industry        VARCHAR(120),
    employees       INT,
    revenue_vnd     NUMERIC(20, 2),
    address         TEXT,
    phone           VARCHAR(40),
    owner_email     VARCHAR(255),
    tags            TEXT[] DEFAULT ARRAY[]::TEXT[],
    properties      JSONB DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crm_companies_ws ON crm_companies(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_companies_domain ON crm_companies(workspace_id, domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_companies_owner ON crm_companies(workspace_id, owner_email);
CREATE INDEX IF NOT EXISTS idx_crm_companies_tags ON crm_companies USING GIN(tags);

-- ─── 2. Contacts ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_contacts (
    id                BIGSERIAL PRIMARY KEY,
    workspace_id      VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email             VARCHAR(255) NOT NULL,
    full_name         VARCHAR(240),
    phone             VARCHAR(40),
    company_id        BIGINT REFERENCES crm_companies(id) ON DELETE SET NULL,
    job_title         VARCHAR(160),
    lifecycle_stage   VARCHAR(20) DEFAULT 'lead'
                      CHECK (lifecycle_stage IN ('lead','mql','sql','customer','evangelist')),
    source            VARCHAR(20) DEFAULT 'manual'
                      CHECK (source IN ('website','import','manual','api','referral')),
    owner_email       VARCHAR(255),
    tags              TEXT[] DEFAULT ARRAY[]::TEXT[],
    properties        JSONB DEFAULT '{}'::JSONB,
    last_activity_at  TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, email)
);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_ws ON crm_contacts(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_company ON crm_contacts(company_id) WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_contacts_owner ON crm_contacts(workspace_id, owner_email);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_stage ON crm_contacts(workspace_id, lifecycle_stage);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_tags ON crm_contacts USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_activity ON crm_contacts(workspace_id, last_activity_at DESC NULLS LAST);

-- ─── 3. Pipelines (each workspace có thể nhiều pipeline) ───────────────────
CREATE TABLE IF NOT EXISTS crm_pipelines (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(160) NOT NULL,
    stages          JSONB NOT NULL DEFAULT '[]'::JSONB,
    -- stages format: [{"id":"new","name":"Mới","probability":10,"position":1}, ...]
    is_default      BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_crm_pipelines_ws ON crm_pipelines(workspace_id);

-- ─── 4. Deals ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_deals (
    id                    BIGSERIAL PRIMARY KEY,
    workspace_id          VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name                  VARCHAR(240) NOT NULL,
    contact_id            BIGINT REFERENCES crm_contacts(id) ON DELETE SET NULL,
    company_id            BIGINT REFERENCES crm_companies(id) ON DELETE SET NULL,
    pipeline_id           BIGINT NOT NULL REFERENCES crm_pipelines(id) ON DELETE CASCADE,
    stage_id              VARCHAR(40) NOT NULL,           -- ref to stages[].id in pipeline
    amount_vnd            NUMERIC(20, 2) DEFAULT 0,
    probability           INT DEFAULT 0,                  -- 0-100
    expected_close_date   DATE,
    actual_close_date     DATE,
    status                VARCHAR(10) DEFAULT 'open'
                          CHECK (status IN ('open','won','lost')),
    owner_email           VARCHAR(255),
    tags                  TEXT[] DEFAULT ARRAY[]::TEXT[],
    properties            JSONB DEFAULT '{}'::JSONB,
    score                 INT DEFAULT 0,                  -- lead/deal score
    lost_reason           VARCHAR(240),
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crm_deals_ws ON crm_deals(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_deals_pipeline ON crm_deals(pipeline_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_crm_deals_contact ON crm_deals(contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_deals_company ON crm_deals(company_id) WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_deals_owner ON crm_deals(workspace_id, owner_email);
CREATE INDEX IF NOT EXISTS idx_crm_deals_status ON crm_deals(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_crm_deals_close ON crm_deals(workspace_id, expected_close_date) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_crm_deals_tags ON crm_deals USING GIN(tags);

-- ─── 5. Activities (calls/emails/meetings/notes/tasks) ─────────────────────
CREATE TABLE IF NOT EXISTS crm_activities (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    contact_id      BIGINT REFERENCES crm_contacts(id) ON DELETE CASCADE,
    deal_id         BIGINT REFERENCES crm_deals(id) ON DELETE CASCADE,
    company_id      BIGINT REFERENCES crm_companies(id) ON DELETE CASCADE,
    type            VARCHAR(20) NOT NULL
                    CHECK (type IN ('call','email','meeting','note','task')),
    subject         VARCHAR(240),
    description     TEXT,
    completed       BOOLEAN DEFAULT FALSE,
    due_at          TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_by      VARCHAR(255),
    metadata        JSONB DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crm_activities_ws ON crm_activities(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_activities_contact ON crm_activities(contact_id, created_at DESC) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_activities_deal ON crm_activities(deal_id, created_at DESC) WHERE deal_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_activities_due ON crm_activities(workspace_id, due_at) WHERE completed = FALSE AND due_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_activities_owner ON crm_activities(workspace_id, created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_activities_type ON crm_activities(workspace_id, type);

-- ─── 6. Tickets (support) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_tickets (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    contact_id      BIGINT REFERENCES crm_contacts(id) ON DELETE SET NULL,
    company_id      BIGINT REFERENCES crm_companies(id) ON DELETE SET NULL,
    subject         VARCHAR(240) NOT NULL,
    description     TEXT,
    status          VARCHAR(20) DEFAULT 'open'
                    CHECK (status IN ('open','pending','resolved','closed')),
    priority        VARCHAR(20) DEFAULT 'normal'
                    CHECK (priority IN ('low','normal','high','urgent')),
    assignee_email  VARCHAR(255),
    source          VARCHAR(40) DEFAULT 'manual',         -- 'email','chat','phone','manual','api'
    properties      JSONB DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_crm_tickets_ws ON crm_tickets(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_tickets_contact ON crm_tickets(contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_crm_tickets_status ON crm_tickets(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_crm_tickets_assignee ON crm_tickets(workspace_id, assignee_email, status);
CREATE INDEX IF NOT EXISTS idx_crm_tickets_priority ON crm_tickets(workspace_id, priority, status);

-- ─── 7. Sequences (email drip) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_sequences (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(160) NOT NULL,
    description     TEXT,
    steps           JSONB NOT NULL DEFAULT '[]'::JSONB,
    -- steps format: [
    --   {"order":1, "wait_days":0, "subject":"Hello {{full_name}}", "body_html":"...", "body_text":"..."},
    --   {"order":2, "wait_days":3, "subject":"Theo dõi", "body_html":"..."},
    --   ...
    -- ]
    active          BOOLEAN DEFAULT TRUE,
    sender_email    VARCHAR(255),
    created_by      VARCHAR(255),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_crm_sequences_ws ON crm_sequences(workspace_id);
CREATE INDEX IF NOT EXISTS idx_crm_sequences_active ON crm_sequences(workspace_id, active);

-- ─── 8. Sequence enrollments ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_sequence_enrollments (
    id              BIGSERIAL PRIMARY KEY,
    sequence_id     BIGINT NOT NULL REFERENCES crm_sequences(id) ON DELETE CASCADE,
    contact_id      BIGINT NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
    workspace_id    VARCHAR(32) NOT NULL,
    current_step    INT DEFAULT 0,                        -- 0-based; step 0 = chưa gửi
    status          VARCHAR(20) DEFAULT 'active'
                    CHECK (status IN ('active','paused','completed','unsubscribed','failed')),
    next_run_at     TIMESTAMPTZ,
    enrolled_at     TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    last_error      TEXT,
    UNIQUE (sequence_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_seq_enroll_ws ON crm_sequence_enrollments(workspace_id);
CREATE INDEX IF NOT EXISTS idx_crm_seq_enroll_seq ON crm_sequence_enrollments(sequence_id, status);
CREATE INDEX IF NOT EXISTS idx_crm_seq_enroll_run ON crm_sequence_enrollments(next_run_at, status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_crm_seq_enroll_contact ON crm_sequence_enrollments(contact_id);

-- ─── 9. Lists (segments) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_lists (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(160) NOT NULL,
    description     TEXT,
    type            VARCHAR(10) NOT NULL DEFAULT 'static'
                    CHECK (type IN ('static','dynamic')),
    -- For dynamic lists: filter is JSON evaluated by crm_engine.evaluate_dynamic_list
    -- example: {"lifecycle_stage":"customer", "tags_any":["vip","whale"], "country":"VN"}
    filter          JSONB DEFAULT '{}'::JSONB,
    member_count    INT DEFAULT 0,                        -- cached
    last_refreshed_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_crm_lists_ws ON crm_lists(workspace_id);

-- ─── 10. List members (static lists) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS crm_list_members (
    list_id         BIGINT NOT NULL REFERENCES crm_lists(id) ON DELETE CASCADE,
    contact_id      BIGINT NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
    added_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (list_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_list_members_contact ON crm_list_members(contact_id);

-- ─── Seed: Default Sales Pipeline cho mỗi workspace hiện có ───────────────
INSERT INTO crm_pipelines (workspace_id, name, stages, is_default)
SELECT id, 'Sales Pipeline', '[
  {"id":"new","name":"Mới","probability":10,"position":1},
  {"id":"qualified","name":"Đủ điều kiện","probability":25,"position":2},
  {"id":"meeting","name":"Họp","probability":50,"position":3},
  {"id":"proposal","name":"Đề xuất","probability":75,"position":4},
  {"id":"negotiation","name":"Đàm phán","probability":85,"position":5},
  {"id":"won","name":"Thắng","probability":100,"position":6}
]'::JSONB, TRUE
FROM workspaces
ON CONFLICT (workspace_id, name) DO NOTHING;
