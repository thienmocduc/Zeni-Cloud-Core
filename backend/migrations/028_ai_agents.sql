-- ============================================================================
-- Migration 028 — AI Agents Library Marketplace
--
-- 50+ pre-built AI agents users can 1-click activate. Each agent =
-- pre-configured: system prompt + tools (router/ocr/translate/vector) +
-- cost ceiling + UI metadata.
--
-- User flow:
--   1. Browse agent_catalog (50+ agents) → filter by category/featured/tier
--   2. POST /agents-library/catalog/{id}/install?ws=...  →  workspace_agents row
--   3. POST /agents-library/workspace/{id}/run?ws=...   →  agent_runs row
--   4. POST /agents-library/reviews?ws=...               →  agent_reviews row
--
-- Tables:
--   agent_catalog       — 50+ pre-built agents (templates)
--   workspace_agents    — installed instances per workspace
--   agent_runs          — full execution history
--   agent_reviews       — 5-star + comment per workspace
-- ============================================================================

-- ─── 1. Agent catalog (50+ pre-built templates) ─────────────────────────────
CREATE TABLE IF NOT EXISTS agent_catalog (
    id VARCHAR(60) PRIMARY KEY,                -- 'customer-support','legal-doc-reviewer'
    name VARCHAR(120) NOT NULL,
    name_vi VARCHAR(120),
    description TEXT,
    description_vi TEXT,
    category VARCHAR(40),                       -- 'support','legal','dev','marketing','data','ops','wellness'
    icon VARCHAR(40),
    system_prompt TEXT NOT NULL,
    default_model VARCHAR(60),
    tools_enabled TEXT[],                       -- ['router','ocr','translate','vector','sms']
    input_schema JSONB,
    output_schema JSONB,
    sample_inputs JSONB,
    pricing_tier VARCHAR(20) DEFAULT 'starter', -- 'free','starter','pro','business'
    cost_per_run_usd NUMERIC(10,6) DEFAULT 0.005,
    avg_latency_ms INT DEFAULT 2000,
    rating NUMERIC(2,1) DEFAULT 4.5,
    install_count INT DEFAULT 0,
    is_featured BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_catalog_category ON agent_catalog(category, is_active);
CREATE INDEX IF NOT EXISTS idx_agent_catalog_featured ON agent_catalog(is_featured, is_active);
CREATE INDEX IF NOT EXISTS idx_agent_catalog_tier ON agent_catalog(pricing_tier, is_active);

-- ─── 2. Workspace agent instances (1-click install from catalog) ────────────
CREATE TABLE IF NOT EXISTS workspace_agents (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    catalog_id VARCHAR(60) NOT NULL REFERENCES agent_catalog(id),
    instance_name VARCHAR(120) NOT NULL,
    custom_system_prompt TEXT,                  -- override catalog if set
    custom_model VARCHAR(60),
    custom_config JSONB,                        -- extra settings per workspace
    is_active BOOLEAN DEFAULT TRUE,
    total_runs INT DEFAULT 0,
    total_cost_usd NUMERIC(10,4) DEFAULT 0,
    last_run_at TIMESTAMPTZ,
    installed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, instance_name)
);
CREATE INDEX IF NOT EXISTS idx_workspace_agents_ws ON workspace_agents(workspace_id, is_active);
CREATE INDEX IF NOT EXISTS idx_workspace_agents_catalog ON workspace_agents(catalog_id);

-- ─── 3. Agent run history ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_runs (
    id BIGSERIAL PRIMARY KEY,
    workspace_agent_id BIGINT NOT NULL REFERENCES workspace_agents(id) ON DELETE CASCADE,
    workspace_id VARCHAR(32) NOT NULL,
    user_email TEXT,
    input_data JSONB NOT NULL,
    output_data JSONB,
    status VARCHAR(20) DEFAULT 'pending',       -- pending | running | success | failed
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INT,
    cost_usd NUMERIC(10,6) DEFAULT 0,
    error_message TEXT,
    routing_decision JSONB                      -- model used + tier + cache
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ws ON agent_runs(workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(workspace_agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, started_at DESC);

-- ─── 4. Agent reviews (5-star + comment) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_reviews (
    id BIGSERIAL PRIMARY KEY,
    catalog_id VARCHAR(60) NOT NULL REFERENCES agent_catalog(id),
    workspace_id VARCHAR(32) NOT NULL,
    user_email TEXT,
    rating INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (catalog_id, workspace_id, user_email)
);
CREATE INDEX IF NOT EXISTS idx_agent_reviews_catalog ON agent_reviews(catalog_id, created_at DESC);

-- ============================================================================
-- ─── Seed 50+ agents (8 support + 6 legal + 8 dev + 8 marketing
--                    + 6 data + 8 ops + 6 wellness = 50) ───────────────────
-- ============================================================================
INSERT INTO agent_catalog (id, name, name_vi, description, description_vi, category, icon, system_prompt, default_model, tools_enabled, pricing_tier, cost_per_run_usd, is_featured) VALUES
  -- ─── Support agents (8) ───────────────────────────────────────────────────
  ('customer-support','Customer Support Bot','Trợ lý hỗ trợ khách hàng','Friendly chatbot for L1 customer queries','Chatbot tiếng Việt trả lời câu hỏi L1','support','headphones','You are a friendly customer support agent for Zeni Cloud. Reply in Vietnamese. Be concise.','sonnet-4-6',ARRAY['router','vector'],'starter',0.003,true),
  ('faq-answerer','FAQ Bot','Bot trả lời FAQ','Auto-answer common questions','Tự động trả lời câu hỏi thường gặp','support','help','Answer based on the provided FAQ knowledge base. Cite source IDs.','haiku-4-5',ARRAY['router','vector'],'starter',0.001,false),
  ('refund-processor','Refund Request Handler','Xử lý hoàn tiền','Triages refund requests, validates eligibility','Xử lý yêu cầu hoàn tiền, kiểm tra điều kiện','support','dollar-sign','Validate refund eligibility per policy. Return JSON {eligible, reason, amount}.','haiku-4-5',ARRAY['router'],'pro',0.005,false),
  ('escalation-router','Ticket Escalation Router','Định tuyến ticket','Routes urgent issues to right team','Định tuyến ticket khẩn đến team phù hợp','support','zap','Classify ticket urgency 1-5 and team (technical/billing/sales).','haiku-4-5',ARRAY['router'],'starter',0.001,false),
  ('chat-summarizer','Chat Conversation Summarizer','Tóm tắt hội thoại','Summarize long support chats','Tóm tắt cuộc hội thoại dài','support','file-text','Summarize the chat in 3 bullet points. Capture issue, attempts, outcome.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('sentiment-analyzer','Customer Sentiment Analyzer','Phân tích cảm xúc khách','Score customer sentiment from messages','Đánh giá cảm xúc khách từ tin nhắn','support','smile','Score sentiment -100 to +100. Return JSON {score, key_phrases, urgency}.','haiku-4-5',ARRAY['router'],'starter',0.001,false),
  ('feedback-categorizer','Feedback Categorizer','Phân loại feedback','Group feedback by topic + sentiment','Nhóm feedback theo chủ đề + cảm xúc','support','tag','Categorize feedback. Return {category, sentiment, action_required}.','haiku-4-5',ARRAY['router'],'starter',0.001,false),
  ('csat-predictor','CSAT Score Predictor','Dự đoán điểm CSAT','Predict CSAT from interaction history','Dự đoán CSAT từ lịch sử tương tác','support','trending-up','Predict CSAT 1-5 based on interaction signals.','haiku-4-5',ARRAY['router'],'pro',0.003,false),

  -- ─── Legal agents (6) ─────────────────────────────────────────────────────
  ('legal-doc-reviewer','Legal Document Reviewer','Soát hợp đồng','Reviews contracts for risks','Soát rủi ro trong hợp đồng','legal','book','You are a legal reviewer. Identify risks, missing clauses, ambiguities. Vietnamese law.','opus-4-7',ARRAY['router','ocr','vector'],'pro',0.05,true),
  ('contract-generator','Contract Generator','Soạn hợp đồng','Generate VN-compliant contracts','Soạn hợp đồng theo luật VN','legal','file-plus','Generate Vietnamese contracts following Luật Lao động 2019, Luật DN 2020.','opus-4-7',ARRAY['router','vector'],'pro',0.04,true),
  ('legal-research','Legal Research Bot','Tra cứu pháp lý','Search VN legal docs (vbpl.vn)','Tra cứu luật VN từ vbpl.vn','legal','search','Search vbpl.vn for relevant laws. Cite article numbers.','sonnet-4-6',ARRAY['router','vector'],'pro',0.01,false),
  ('clause-comparer','Contract Clause Comparer','So sánh điều khoản','Diff clauses across contracts','So sánh điều khoản giữa các hợp đồng','legal','git-pull-request','Compare 2 contract clauses. Output diff + risk score.','sonnet-4-6',ARRAY['router'],'pro',0.008,false),
  ('compliance-checker','Compliance Checker','Kiểm tra tuân thủ','Check doc against ND 13/2023','Kiểm tra tài liệu theo NĐ 13/2023','legal','shield','Check compliance with Nghị định 13/2023 personal data protection.','sonnet-4-6',ARRAY['router','vector'],'pro',0.008,false),
  ('ipo-doc-reviewer','IPO Document Reviewer','Soát hồ sơ IPO','Review S-1, prospectus drafts','Soát S-1, prospectus','legal','trending-up','Review IPO docs for SEC compliance issues. Flag every concern.','opus-4-7',ARRAY['router','vector'],'business',0.10,false),

  -- ─── Dev agents (8) ───────────────────────────────────────────────────────
  ('code-reviewer','Code Reviewer','Soát code','PR-style code review with suggestions','Code review style PR có gợi ý','dev','code','Review code for bugs, security, performance. Suggest fixes.','sonnet-4-6',ARRAY['router'],'pro',0.005,true),
  ('sql-generator','SQL Generator','Sinh SQL','Natural language → SQL','Tiếng Việt/Anh → SQL','dev','database','Generate Postgres SQL from description. Use parameterized queries.','sonnet-4-6',ARRAY['router'],'starter',0.003,true),
  ('test-writer','Test Case Writer','Viết test cases','Generate pytest/jest test suites','Tạo bộ test pytest/jest','dev','check-circle','Generate comprehensive test cases including edge cases.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('bug-triage','Bug Triage Bot','Phân loại bug','Categorize + assign bugs','Phân loại + assign bug','dev','alert-circle','Triage bug reports: severity, component, suggested assignee.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('docstring-writer','Docstring Writer','Viết docstring','Auto-generate Python/JS docstrings','Tự sinh docstring','dev','book-open','Write clear docstrings with params, returns, examples.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('refactor-suggester','Refactor Suggester','Đề xuất refactor','Suggest cleaner code structures','Đề xuất cấu trúc code sạch hơn','dev','git-merge','Suggest refactor while preserving behavior. Provide diff.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('security-scanner','Security Code Scanner','Quét bảo mật','Find OWASP Top 10 vulns','Tìm lỗ hổng OWASP Top 10','dev','shield','Scan for SQL injection, XSS, CSRF, insecure deserialization.','sonnet-4-6',ARRAY['router'],'pro',0.008,false),
  ('api-doc-generator','API Doc Generator','Sinh API docs','OpenAPI from code','OpenAPI từ code','dev','file-code','Generate OpenAPI 3.0 spec from FastAPI/Express code.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),

  -- ─── Marketing agents (8) ─────────────────────────────────────────────────
  ('copywriter','Marketing Copywriter','Viết quảng cáo','Ad copy + landing page text','Viết content quảng cáo + landing','marketing','edit','Write punchy Vietnamese ad copy. Match brand voice. Include CTA.','sonnet-4-6',ARRAY['router'],'starter',0.003,true),
  ('seo-analyzer','SEO Content Analyzer','Phân tích SEO','Score + improve SEO','Đánh giá + cải thiện SEO','marketing','trending-up','Score SEO. Suggest title tags, meta, keywords for VN market.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('email-campaigner','Email Campaign Writer','Viết email marketing','Subject + body for campaigns','Subject + body cho campaign','marketing','mail','Write email campaigns. A/B test variations. Vietnamese-first.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('social-poster','Social Media Post Generator','Sinh post social','Tweets, FB posts, LinkedIn','Tweet, FB, LinkedIn','marketing','share-2','Generate social posts in brand voice. Include hashtags VN.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('audience-segmenter','Audience Segmenter','Phân khúc khách','Cluster customers from data','Cụm khách từ data','marketing','users','Analyze customer data to suggest segments + personas.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('campaign-optimizer','Campaign Performance Analyzer','Phân tích chiến dịch','ROI + suggested actions','ROI + đề xuất action','marketing','bar-chart-2','Analyze campaign metrics. Suggest optimization actions.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('product-namer','Product Name Generator','Đặt tên sản phẩm','Brainstorm product names','Brainstorm tên sản phẩm','marketing','tag','Generate 10 product name candidates in Vietnamese + reasons.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('brand-guidelines','Brand Guidelines Writer','Viết brand guideline','Generate brand voice docs','Sinh tài liệu brand voice','marketing','briefcase','Generate comprehensive brand guidelines doc.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),

  -- ─── Data agents (6) ──────────────────────────────────────────────────────
  ('data-cleaner','Data Cleaner','Làm sạch dữ liệu','Clean messy CSV/spreadsheets','Làm sạch CSV/spreadsheet','data','filter','Clean data: dedupe, normalize, fix formats. Return SQL or Python script.','sonnet-4-6',ARRAY['router'],'pro',0.005,true),
  ('csv-analyzer','CSV Analyzer','Phân tích CSV','Stats + insights from CSV','Stats + insights từ CSV','data','grid','Analyze CSV. Provide stats, anomalies, suggested charts.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('chart-suggester','Chart Suggester','Đề xuất biểu đồ','Pick best chart for data','Chọn biểu đồ phù hợp data','data','pie-chart','Suggest 3 chart types with code (Vega-Lite or Plotly).','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('etl-builder','ETL Pipeline Builder','Build ETL','Generate ETL scripts','Tạo script ETL','data','git-branch','Build ETL pipeline (Python/SQL) from source-target description.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('insight-generator','Business Insight Generator','Sinh insight','Insights from BI dashboards','Sinh insight từ dashboard BI','data','eye','Analyze metrics. Generate 5 actionable insights.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('forecast-model','Sales Forecaster','Dự báo doanh số','Time-series forecast','Dự báo time-series','data','trending-up','Forecast sales using ARIMA/Prophet logic. Output: forecast + confidence.','sonnet-4-6',ARRAY['router'],'pro',0.008,false),

  -- ─── Ops agents (8) ───────────────────────────────────────────────────────
  ('meeting-summarizer','Meeting Summarizer','Tóm tắt họp','Summary + action items from transcripts','Tóm tắt + action item từ transcript','ops','clock','Summarize meeting. Extract: decisions, action items (owner+deadline), risks.','sonnet-4-6',ARRAY['router'],'starter',0.005,true),
  ('email-composer','Email Composer','Viết email','Compose pro emails','Viết email chuyên nghiệp','ops','mail','Compose professional emails. Match tone (formal/friendly).','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('schedule-optimizer','Schedule Optimizer','Tối ưu lịch','Find best meeting times','Tìm khung giờ họp tối ưu','ops','calendar','Suggest optimal meeting times across timezones.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('task-prioritizer','Task Prioritizer','Sắp xếp ưu tiên','Eisenhower matrix + urgency','Eisenhower matrix','ops','list','Prioritize tasks: Eisenhower matrix + estimated effort.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('expense-categorizer','Expense Categorizer','Phân loại chi phí','Auto-categorize receipts','Tự phân loại hoá đơn','ops','credit-card','Categorize expenses (rent/salary/marketing/...). Use VN VAT rules.','haiku-4-5',ARRAY['router','ocr'],'starter',0.003,false),
  ('hr-screener','HR Resume Screener','Sàng lọc CV','Score resumes vs job description','Đánh giá CV theo JD','ops','user-check','Score CV vs JD. Output: fit score + key skills + concerns.','sonnet-4-6',ARRAY['router','ocr'],'pro',0.008,false),
  ('inventory-manager','Inventory Reorder Agent','Đặt hàng tồn kho','Reorder logic from stock data','Logic đặt hàng từ data tồn','ops','package','Suggest reorder qty. Use historical sales velocity.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('vendor-evaluator','Vendor Evaluator','Đánh giá nhà cung cấp','Score vendors on multi-criteria','Đánh giá NCC đa tiêu chí','ops','award','Evaluate vendors. Score: price, quality, reliability, support.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),

  -- ─── Wellness agents (6) — for ANIMA Care ─────────────────────────────────
  ('wellness-advisor','Wellness Advisor','Tư vấn sức khỏe','Personalized wellness tips','Tư vấn sức khỏe cá nhân hóa','wellness','heart','Vietnamese wellness advice. Diet, exercise, mental health. Disclaim: not medical.','sonnet-4-6',ARRAY['router','vector'],'starter',0.003,false),
  ('symptom-triage','Symptom Triage','Triage triệu chứng','Suggest urgency level','Đề xuất mức khẩn','wellness','activity','Triage symptoms: home care / GP / ER. Always recommend professional help.','sonnet-4-6',ARRAY['router'],'starter',0.003,false),
  ('meal-planner','Meal Planner','Lập thực đơn','Vietnamese meal plans','Thực đơn Việt cá nhân hóa','wellness','coffee','Plan VN meals based on dietary needs + goals. Include shopping list.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('workout-builder','Workout Builder','Xây giáo án','Custom exercise programs','Giáo án tập tùy chỉnh','wellness','play','Build workout: warmup, main, cooldown. Match fitness level.','haiku-4-5',ARRAY['router'],'starter',0.002,false),
  ('mood-tracker','Mood Tracker Insights','Theo dõi tâm trạng','Pattern analysis from journals','Phân tích pattern từ nhật ký','wellness','smile','Analyze mood journal entries. Identify triggers + suggest interventions.','sonnet-4-6',ARRAY['router'],'pro',0.005,false),
  ('sleep-coach','Sleep Coach','Huấn luyện giấc ngủ','Sleep optimization advice','Tối ưu giấc ngủ','wellness','moon','Analyze sleep patterns. Suggest sleep hygiene improvements.','haiku-4-5',ARRAY['router'],'starter',0.002,false)
ON CONFLICT (id) DO NOTHING;
