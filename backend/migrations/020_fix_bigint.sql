-- Fix: quota_ai_tokens INT (max 2.1 tỷ) không đủ cho Enterprise (100 tỷ tokens/tháng)
ALTER TABLE pricing_plans ALTER COLUMN quota_ai_tokens_per_month TYPE BIGINT;
ALTER TABLE pricing_plans ALTER COLUMN quota_requests_per_month TYPE BIGINT;

-- Re-insert seed 5 tiers
INSERT INTO pricing_plans (id, name, price_vnd_monthly, price_usd_monthly, quota_requests_per_month, quota_ai_tokens_per_month, quota_storage_gb, quota_router_usd_per_month, quota_projects, quota_dev_seats, sla_uptime_percent, support_level, custom_domain, features, sort_order)
VALUES
  ('free',       'Free — Khám phá',         0,         0.00,    100000,     5000000,      1,    0.50,   1,  1, 99.0,  'community', false, ARRAY['ai_basic','ocr','translate','vector','sms','slack','privacy_tier1'], 1),
  ('starter',    'Starter — Khởi tạo',      999000,    40.00,   1000000,    100000000,    10,   5.00,   5,  3, 99.5,  'email',     true,  ARRAY['ai_full','ocr','translate','vector','sms','slack','custom_domain','privacy_tier1'], 2),
  ('pro',        'Pro — Doanh nghiệp',      4900000,   200.00,  10000000,   1000000000,   100,  50.00,  -1, 10, 99.9,  'priority',  true,  ARRAY['ai_full','ocr','translate','vector','sms','slack','custom_domain','privacy_tier2','smart_contract','vertical_models'], 3),
  ('business',   'Business — Tập đoàn',     49000000,  2000.00, 100000000,  10000000000,  1024, 500.00, -1, 50, 99.95, 'dedicated', true,  ARRAY['all','dedicated_csm','sla','privacy_tier3','soc2_pending'], 4),
  ('enterprise', 'Enterprise — Riêng biệt', 199000000, 8000.00, 1000000000, 100000000000, 10240,5000.00,-1,500,99.99,'24x7_phone',true, ARRAY['all','dedicated_infra','on_prem','iso27001','soc2','gdpr','privacy_tier4'], 5)
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name, price_vnd_monthly = EXCLUDED.price_vnd_monthly,
  price_usd_monthly = EXCLUDED.price_usd_monthly,
  quota_requests_per_month = EXCLUDED.quota_requests_per_month,
  quota_ai_tokens_per_month = EXCLUDED.quota_ai_tokens_per_month,
  quota_storage_gb = EXCLUDED.quota_storage_gb,
  quota_router_usd_per_month = EXCLUDED.quota_router_usd_per_month,
  quota_projects = EXCLUDED.quota_projects,
  quota_dev_seats = EXCLUDED.quota_dev_seats,
  features = EXCLUDED.features;

-- Default Free tier cho mọi workspace cũ chưa có subscription
INSERT INTO workspace_subscriptions (workspace_id, plan_id, status)
SELECT id, 'free', 'active' FROM workspaces
WHERE id NOT IN (SELECT workspace_id FROM workspace_subscriptions)
ON CONFLICT (workspace_id) DO NOTHING;
