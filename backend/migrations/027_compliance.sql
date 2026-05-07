-- ============================================================================
-- Migration 027 — Compliance Pack (SOC 2, ISO 27001, GDPR, Nghị định 13/2023 VN)
--
-- Compliance Pack cho enterprise customers: tự động hoá việc quản lý kiểm
-- soát, thu thập evidence, audit trail, risk register và policies. Cho phép
-- workspace self-serve dashboard + auto evidence collection.
--
-- Tables:
--   compliance_frameworks      — Khung tuân thủ (SOC 2, ISO 27001, GDPR, ND13)
--   compliance_controls        — Các kiểm soát của từng khung (e.g. SOC 2 CC6.1)
--   compliance_assessments     — Đánh giá kiểm soát theo workspace
--   compliance_evidence        — Bằng chứng đính kèm (logs, screenshots, docs)
--   compliance_audit_trail     — Audit log riêng cho compliance evidence
--   compliance_risks           — Risk register (likelihood × impact)
--   compliance_policies        — Chính sách & quy trình
--
-- Dùng cho:
--   - Self-serve compliance dashboard
--   - Auto evidence collection từ Cloud SQL / GCS / audit_log
--   - Audit-ready report (PDF/zip) cho external auditors
-- ============================================================================

-- ─── 1. Frameworks (khung tuân thủ) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compliance_frameworks (
    id VARCHAR(40) PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    description TEXT,
    version VARCHAR(40),
    categories TEXT[],
    is_active BOOLEAN DEFAULT TRUE
);

-- ─── 2. Controls (kiểm soát của từng khung) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS compliance_controls (
    id BIGSERIAL PRIMARY KEY,
    framework_id VARCHAR(40) NOT NULL REFERENCES compliance_frameworks(id) ON DELETE CASCADE,
    control_code VARCHAR(40) NOT NULL,        -- 'CC6.1','A.5.1.1','Article 32'
    title VARCHAR(200) NOT NULL,
    description TEXT,
    category VARCHAR(60),
    severity VARCHAR(20) DEFAULT 'medium',
    automation_type VARCHAR(40),               -- 'automatic','semi-auto','manual'
    UNIQUE (framework_id, control_code)
);
CREATE INDEX IF NOT EXISTS idx_compliance_controls_fw ON compliance_controls(framework_id, category);

-- ─── 3. Assessments (đánh giá kiểm soát theo workspace) ─────────────────────
CREATE TABLE IF NOT EXISTS compliance_assessments (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    framework_id VARCHAR(40) NOT NULL,
    control_id BIGINT NOT NULL REFERENCES compliance_controls(id),
    status VARCHAR(20) DEFAULT 'not_started', -- 'not_started','in_progress','compliant','non_compliant','exempt'
    evidence_count INT DEFAULT 0,
    last_check_at TIMESTAMPTZ,
    next_review_at TIMESTAMPTZ,
    assigned_to TEXT,
    notes TEXT,
    auto_check_passed BOOLEAN,
    UNIQUE (workspace_id, control_id)
);
CREATE INDEX IF NOT EXISTS idx_compliance_assess_ws ON compliance_assessments(workspace_id, framework_id, status);

-- ─── 4. Evidence (bằng chứng) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compliance_evidence (
    id BIGSERIAL PRIMARY KEY,
    assessment_id BIGINT NOT NULL REFERENCES compliance_assessments(id) ON DELETE CASCADE,
    workspace_id VARCHAR(32) NOT NULL,
    evidence_type VARCHAR(40),                -- 'audit_log','screenshot','policy_doc','attestation','test_result'
    title VARCHAR(200),
    description TEXT,
    storage_url TEXT,                          -- GCS or external
    metadata JSONB,
    collected_at TIMESTAMPTZ DEFAULT NOW(),
    collected_by TEXT,
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_compliance_evid_assess ON compliance_evidence(assessment_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_compliance_evid_ws ON compliance_evidence(workspace_id, collected_at DESC);

-- ─── 5. Compliance Audit Trail (riêng — không trùng app audit_log) ──────────
CREATE TABLE IF NOT EXISTS compliance_audit_trail (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    actor_email TEXT,
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(60),
    resource_id TEXT,
    ip_address INET,
    user_agent TEXT,
    request_data JSONB,
    response_status INT,
    occurred_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_compliance_audit_ws ON compliance_audit_trail(workspace_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_compliance_audit_action ON compliance_audit_trail(action, occurred_at DESC);

-- ─── 6. Risk Register ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compliance_risks (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    likelihood VARCHAR(20),                    -- 'rare','unlikely','possible','likely','almost_certain'
    impact VARCHAR(20),                        -- 'insignificant','minor','moderate','major','catastrophic'
    risk_score INT,                            -- 1-25
    status VARCHAR(20) DEFAULT 'open',         -- 'open','accepted','mitigated','closed'
    treatment_plan TEXT,
    owner_email TEXT,
    review_date DATE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_compliance_risks_ws ON compliance_risks(workspace_id, status);

-- ─── 7. Policies & Procedures ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS compliance_policies (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    category VARCHAR(60),                      -- 'security','privacy','hr','ops'
    version VARCHAR(20),
    content_md TEXT,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    next_review_date DATE,
    status VARCHAR(20) DEFAULT 'draft',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_compliance_policies_ws ON compliance_policies(workspace_id, status);

-- ============================================================================
-- SEED DATA
-- ============================================================================

-- Seed 4 frameworks
INSERT INTO compliance_frameworks (id, name, description, version, categories) VALUES
  ('soc2', 'SOC 2 Type II', 'Trust Services Criteria for security, availability, confidentiality', '2017', ARRAY['security','availability','confidentiality']),
  ('iso27001', 'ISO 27001:2022', 'Information security management system', '2022', ARRAY['isms','controls','risk']),
  ('gdpr', 'GDPR', 'EU General Data Protection Regulation', '2018', ARRAY['data_protection','privacy','consent']),
  ('nd13', 'Nghị định 13/2023/NĐ-CP', 'Bảo vệ dữ liệu cá nhân Việt Nam', '2023', ARRAY['data_protection','consent_vn'])
ON CONFLICT (id) DO NOTHING;

-- Seed key SOC 2 controls (15 most common)
INSERT INTO compliance_controls (framework_id, control_code, title, category, automation_type) VALUES
  ('soc2','CC1.1','Demonstrates commitment to integrity','governance','manual'),
  ('soc2','CC2.1','Information & communication','governance','manual'),
  ('soc2','CC6.1','Logical access controls','security','automatic'),
  ('soc2','CC6.2','User access provisioning','security','automatic'),
  ('soc2','CC6.3','Unauthorized access prevention','security','automatic'),
  ('soc2','CC6.6','Data encryption at rest','security','automatic'),
  ('soc2','CC6.7','Data encryption in transit','security','automatic'),
  ('soc2','CC7.1','Vulnerability detection','security','semi-auto'),
  ('soc2','CC7.2','Security incident monitoring','security','automatic'),
  ('soc2','CC7.3','Incident response','security','semi-auto'),
  ('soc2','CC8.1','Change management','ops','semi-auto'),
  ('soc2','A1.1','Capacity management','availability','automatic'),
  ('soc2','A1.2','Backup procedures','availability','automatic'),
  ('soc2','A1.3','Disaster recovery','availability','semi-auto'),
  ('soc2','C1.1','Confidentiality of data','confidentiality','automatic')
ON CONFLICT (framework_id, control_code) DO NOTHING;

-- ISO 27001 (top 10 Annex A controls)
INSERT INTO compliance_controls (framework_id, control_code, title, category, automation_type) VALUES
  ('iso27001','A.5.1','Information security policies','governance','manual'),
  ('iso27001','A.6.1','Internal organization','governance','manual'),
  ('iso27001','A.8.1','Asset management','assets','semi-auto'),
  ('iso27001','A.9.1','Access control policy','security','automatic'),
  ('iso27001','A.10.1','Cryptographic controls','security','automatic'),
  ('iso27001','A.12.1','Operations procedures','ops','semi-auto'),
  ('iso27001','A.13.1','Network security','security','automatic'),
  ('iso27001','A.14.1','System acquisition','dev','semi-auto'),
  ('iso27001','A.16.1','Security incident management','incidents','semi-auto'),
  ('iso27001','A.18.1','Compliance with legal requirements','compliance','manual')
ON CONFLICT (framework_id, control_code) DO NOTHING;

-- GDPR top articles
INSERT INTO compliance_controls (framework_id, control_code, title, category, automation_type) VALUES
  ('gdpr','Art.5','Principles of data processing','privacy','manual'),
  ('gdpr','Art.6','Lawful basis for processing','privacy','manual'),
  ('gdpr','Art.13','Right to information','privacy','automatic'),
  ('gdpr','Art.15','Right of access','privacy','automatic'),
  ('gdpr','Art.17','Right to erasure','privacy','automatic'),
  ('gdpr','Art.20','Right to portability','privacy','automatic'),
  ('gdpr','Art.32','Security of processing','security','automatic'),
  ('gdpr','Art.33','Breach notification 72h','incidents','semi-auto'),
  ('gdpr','Art.35','DPIA','privacy','manual')
ON CONFLICT (framework_id, control_code) DO NOTHING;

-- Nghị định 13/2023 VN (key articles)
INSERT INTO compliance_controls (framework_id, control_code, title, category, automation_type) VALUES
  ('nd13','D9','Sự đồng ý của chủ thể dữ liệu','consent_vn','automatic'),
  ('nd13','D11','Thông báo xử lý dữ liệu','privacy','automatic'),
  ('nd13','D14','Quyền của chủ thể dữ liệu','privacy','automatic'),
  ('nd13','D15','Xoá dữ liệu cá nhân','privacy','automatic'),
  ('nd13','D17','Bảo mật dữ liệu cá nhân','security','automatic'),
  ('nd13','D24','Đánh giá tác động xử lý DLCN','privacy','manual'),
  ('nd13','D25','Chuyển dữ liệu ra nước ngoài','privacy','manual'),
  ('nd13','D38','Thông báo vi phạm bảo vệ DLCN','incidents','semi-auto')
ON CONFLICT (framework_id, control_code) DO NOTHING;
