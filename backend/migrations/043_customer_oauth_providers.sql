-- Migration 043 — Customer-Provided OAuth Providers
-- Cho phép khách của Zeni Cloud cài key OAuth của HỌ (Zalo, Apple, Facebook, Line, Kakao, TikTok, generic)
-- Zeni Cloud lưu encrypted (KMS), expose endpoint /auth/{provider}/{workspace_id}/{login|callback}
-- App của khách dùng nút "Đăng nhập Zalo" → flow OAuth qua endpoint Zeni Cloud → trả token về app khách

CREATE TABLE IF NOT EXISTS workspace_oauth_providers (
  id              BIGSERIAL PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  provider        TEXT NOT NULL,  -- 'zalo' | 'apple' | 'facebook' | 'line' | 'kakao' | 'tiktok' | 'linkedin' | 'generic'
  display_name    TEXT NOT NULL,
  -- Credentials (encrypted via Cloud KMS — never stored plaintext)
  client_id       TEXT NOT NULL,
  client_secret_encrypted BYTEA NOT NULL,  -- KMS-wrapped
  -- OAuth config
  auth_url        TEXT,
  token_url       TEXT,
  userinfo_url    TEXT,
  scopes          TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
  redirect_uri    TEXT NOT NULL,  -- callback URL khách config khi tạo app trên Zalo/etc
  -- App-side config
  app_callback_url TEXT NOT NULL,  -- nơi Zeni Cloud redirect về sau khi login OK
  app_origin      TEXT,  -- CORS allow-origin của app khách
  -- State
  enabled         BOOLEAN NOT NULL DEFAULT TRUE,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  -- Audit
  created_by      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Một workspace chỉ có 1 cấu hình cho 1 provider
  UNIQUE (workspace_id, provider)
);

CREATE INDEX IF NOT EXISTS idx_oauth_providers_ws        ON workspace_oauth_providers (workspace_id);
CREATE INDEX IF NOT EXISTS idx_oauth_providers_provider  ON workspace_oauth_providers (provider) WHERE enabled = TRUE;

-- Login attempts log (audit + rate-limit)
CREATE TABLE IF NOT EXISTS oauth_login_attempts (
  id              BIGSERIAL PRIMARY KEY,
  workspace_id    TEXT NOT NULL,
  provider        TEXT NOT NULL,
  state_token     TEXT NOT NULL,  -- CSRF protection
  ip_address      INET,
  user_agent      TEXT,
  status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'success' | 'failed' | 'expired'
  external_user_id TEXT,
  external_email  TEXT,
  error_message   TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_oauth_attempts_ws      ON oauth_login_attempts (workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_oauth_attempts_state   ON oauth_login_attempts (state_token) WHERE status = 'pending';

-- Provider templates (built-in defaults — khách không phải nhớ URL)
CREATE TABLE IF NOT EXISTS oauth_provider_templates (
  provider        TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  auth_url        TEXT NOT NULL,
  token_url       TEXT NOT NULL,
  userinfo_url    TEXT,
  default_scopes  TEXT[] NOT NULL,
  docs_url        TEXT,
  setup_guide     TEXT
);

INSERT INTO oauth_provider_templates (provider, display_name, auth_url, token_url, userinfo_url, default_scopes, docs_url, setup_guide) VALUES
  ('zalo',     'Zalo',     'https://oauth.zaloapp.com/v4/permission', 'https://oauth.zaloapp.com/v4/access_token', 'https://graph.zalo.me/v2.0/me', ARRAY['id','name','picture'], 'https://developers.zalo.me/docs/social/sign-in/log-in-using-permission-code', 'Đăng ký app tại developers.zalo.me, tạo OAuth, paste App ID + Secret Key vào đây'),
  ('apple',    'Apple',    'https://appleid.apple.com/auth/authorize', 'https://appleid.apple.com/auth/token', NULL, ARRAY['name','email'], 'https://developer.apple.com/sign-in-with-apple/get-started/', 'Đăng ký Apple Developer ($99/year), tạo Service ID + Key, paste vào đây'),
  ('facebook', 'Facebook', 'https://www.facebook.com/v18.0/dialog/oauth', 'https://graph.facebook.com/v18.0/oauth/access_token', 'https://graph.facebook.com/me', ARRAY['email','public_profile'], 'https://developers.facebook.com/docs/facebook-login/web', 'Tạo app tại developers.facebook.com, lấy App ID + App Secret, paste vào đây'),
  ('line',     'LINE',     'https://access.line.me/oauth2/v2.1/authorize', 'https://api.line.me/oauth2/v2.1/token', 'https://api.line.me/v2/profile', ARRAY['profile','openid','email'], 'https://developers.line.biz/en/docs/line-login/', 'Tạo Channel tại developers.line.biz, lấy Channel ID + Secret, paste vào đây'),
  ('kakao',    'Kakao',    'https://kauth.kakao.com/oauth/authorize', 'https://kauth.kakao.com/oauth/token', 'https://kapi.kakao.com/v2/user/me', ARRAY['profile_nickname','account_email'], 'https://developers.kakao.com/docs/latest/en/kakaologin/common', 'Đăng ký Kakao Developers, tạo app, paste REST API Key + Secret'),
  ('tiktok',   'TikTok',   'https://www.tiktok.com/v2/auth/authorize/', 'https://open.tiktokapis.com/v2/oauth/token/', 'https://open.tiktokapis.com/v2/user/info/', ARRAY['user.info.basic'], 'https://developers.tiktok.com/doc/login-kit-web', 'Đăng ký TikTok for Developers, tạo app, paste Client Key + Secret'),
  ('linkedin', 'LinkedIn', 'https://www.linkedin.com/oauth/v2/authorization', 'https://www.linkedin.com/oauth/v2/accessToken', 'https://api.linkedin.com/v2/userinfo', ARRAY['openid','profile','email'], 'https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2', 'Tạo app tại linkedin.com/developers, paste Client ID + Secret')
ON CONFLICT (provider) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  auth_url = EXCLUDED.auth_url,
  token_url = EXCLUDED.token_url,
  userinfo_url = EXCLUDED.userinfo_url,
  default_scopes = EXCLUDED.default_scopes,
  docs_url = EXCLUDED.docs_url,
  setup_guide = EXCLUDED.setup_guide;
