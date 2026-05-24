-- Migration 067 — Add Google OAuth Provider Template
-- Cho phép khách Zeni Cloud đăng ký Google OAuth qua nền tảng Zeni
-- thay vì phải tự cấu hình trên Google Console.
--
-- Flow: Khách paste Google OAuth client_id + client_secret vào Zeni Dashboard
--       → Zeni encrypt KMS → expose endpoint /auth/google/{ws}/login
--       → User login Google → Zeni redirect về app khách với token

-- 1. Add Google template
INSERT INTO oauth_provider_templates (provider, display_name, auth_url, token_url, userinfo_url, default_scopes, docs_url, setup_guide) VALUES
  ('google',   'Google',   'https://accounts.google.com/o/oauth2/v2/auth', 'https://oauth2.googleapis.com/token', 'https://www.googleapis.com/oauth2/v3/userinfo', ARRAY['openid','email','profile'], 'https://developers.google.com/identity/protocols/oauth2', 'Tạo project tại console.cloud.google.com → APIs & Services → Credentials → OAuth 2.0 Client ID → paste Client ID + Secret vào đây. Hoặc liên hệ Zeni Cloud CTO để được hỗ trợ cấu hình.')
ON CONFLICT (provider) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  auth_url = EXCLUDED.auth_url,
  token_url = EXCLUDED.token_url,
  userinfo_url = EXCLUDED.userinfo_url,
  default_scopes = EXCLUDED.default_scopes,
  docs_url = EXCLUDED.docs_url,
  setup_guide = EXCLUDED.setup_guide;

-- 2. Update comment on workspace_oauth_providers to include Google
COMMENT ON TABLE workspace_oauth_providers IS 'Customer-provided OAuth providers (Google, Zalo, Apple, Facebook, Line, Kakao, TikTok, LinkedIn, Generic). Zeni Cloud acts as OAuth middleware.';
