-- ============================================================================
-- Migration 081 — Design Sign-off Workflow + KTS Certified Partners
--
-- Phase 3 — Task #21 · Chairman approved 2026-05-26.
--
-- Tables:
--   design_sessions          — persist orchestrator results (for artifacts/signoff)
--   design_artifacts         — CAD + Excel files generated per session
--   kts_certified_partners   — KTS chứng chỉ A/B/C trên platform
--   design_signoff_requests  — request a KTS to sign a design package
--
-- All idempotent via CREATE IF NOT EXISTS + INSERT ON CONFLICT.
-- ============================================================================

-- ─── 1. Design sessions persistence ────────────────────────────
CREATE TABLE IF NOT EXISTS design_sessions (
    id UUID PRIMARY KEY,
    workspace_id VARCHAR(64) NOT NULL,
    actor_email VARCHAR(255),
    brief TEXT,
    style_choice VARCHAR(32),
    num_floors INT,
    num_residents INT,
    location_province VARCHAR(64),
    verdict VARCHAR(32),                          -- pending|ready_for_signoff|needs_revision|major_issues
    agent_outputs JSONB,
    metrics JSONB,
    errors JSONB,
    duration_ms INT,
    total_cost_usd NUMERIC(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_design_sessions_ws ON design_sessions(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_design_sessions_verdict ON design_sessions(verdict);


-- ─── 2. Design artifacts (CAD/BOQ files) ───────────────────────
CREATE TABLE IF NOT EXISTS design_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    content_type VARCHAR(80) DEFAULT 'application/octet-stream',
    artifact_kind VARCHAR(40),                    -- 'cad_floor_plan','cad_section','cad_elevation','cad_struct','cad_elec','cad_water','boq_excel','full_zip'
    gcs_bucket VARCHAR(120),
    gcs_key VARCHAR(512),
    size_bytes INT,
    sha256 VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT fk_design_artifact_session
      FOREIGN KEY (session_id) REFERENCES design_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_design_artifacts_session ON design_artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_design_artifacts_ws ON design_artifacts(workspace_id, created_at DESC);


-- ─── 3. KTS certified partners ─────────────────────────────────
CREATE TABLE IF NOT EXISTS kts_certified_partners (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name VARCHAR(255) NOT NULL,
    cert_id VARCHAR(80) NOT NULL,                 -- "KTS-A-Hà Nội-001234"
    cert_authority VARCHAR(120),                  -- "Sở Xây dựng Hà Nội"
    cert_expires DATE,
    specialty VARCHAR(40)[],                      -- ['kien_truc','noi_that','ket_cau','mep','boq']
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(32),
    fee_per_project_vnd BIGINT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_kts_partners_active ON kts_certified_partners(is_active);
CREATE INDEX IF NOT EXISTS idx_kts_partners_specialty ON kts_certified_partners USING GIN(specialty);


-- ─── 4. Design sign-off requests ───────────────────────────────
CREATE TABLE IF NOT EXISTS design_signoff_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    requester_email VARCHAR(255),
    kts_partner_id UUID NOT NULL,
    urgency VARCHAR(20) DEFAULT 'normal',         -- 'normal','urgent','same_day'
    status VARCHAR(20) DEFAULT 'pending',         -- 'pending','signed','declined','expired'
    requested_at TIMESTAMPTZ DEFAULT NOW(),
    signed_at TIMESTAMPTZ NULL,
    declined_reason TEXT NULL,
    signed_artifacts JSONB,                       -- {artifact_id: {sha256, signed_blob_b64}}
    blockchain_anchor_tx VARCHAR(120) NULL,
    blockchain_anchor_chain VARCHAR(40) DEFAULT 'polygon',
    notification_sent_at TIMESTAMPTZ NULL,
    CONSTRAINT fk_signoff_session
      FOREIGN KEY (session_id) REFERENCES design_sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_signoff_partner
      FOREIGN KEY (kts_partner_id) REFERENCES kts_certified_partners(id)
);
CREATE INDEX IF NOT EXISTS idx_signoff_session ON design_signoff_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_signoff_partner ON design_signoff_requests(kts_partner_id);
CREATE INDEX IF NOT EXISTS idx_signoff_ws_status ON design_signoff_requests(workspace_id, status);


-- ─── 5. Seed 3 demo KTS partners ───────────────────────────────
INSERT INTO kts_certified_partners
  (id, full_name, cert_id, cert_authority, cert_expires, specialty, email, phone, fee_per_project_vnd, is_active) VALUES
  ('00000000-0000-0000-0000-00000000a001'::uuid,
   'KTS Nguyễn Văn An', 'KTS-A-HN-001234', 'Sở Xây dựng Hà Nội',
   '2028-12-31', ARRAY['kien_truc','noi_that'],
   'kts.an@vietcontech.demo', '+84-901-234-001', 8000000, TRUE),
  ('00000000-0000-0000-0000-00000000a002'::uuid,
   'KSCT Trần Thị Bình', 'KSCT-B-HCM-005678', 'Sở Xây dựng TP HCM',
   '2027-06-30', ARRAY['ket_cau','mep'],
   'ksct.binh@vietcontech.demo', '+84-902-345-002', 12000000, TRUE),
  ('00000000-0000-0000-0000-00000000a003'::uuid,
   'KS Định mức Lê Văn Cảnh', 'KS-BOQ-HN-009012', 'Hội Giá Xây dựng VN',
   '2029-03-15', ARRAY['boq'],
   'ks.canh@vietcontech.demo', '+84-903-456-003', 5000000, TRUE)
ON CONFLICT (id) DO NOTHING;

-- end of file
