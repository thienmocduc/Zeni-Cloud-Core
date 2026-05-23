-- Migration 067: Add Google OAuth provider template
-- Inserts Google OAuth template into oauth_provider_templates table

INSERT INTO oauth_provider_templates (
    provider,
    display_name,
    auth_url,
    token_url,
    userinfo_url,
    default_scopes,
    icon_url,
    documentation_url
  ) VALUES (
    'google',
    'Google',
    'https://accounts.google.com/o/oauth2/v2/auth',
    'https://oauth2.googleapis.com/token',
    'https://www.googleapis.com/oauth2/v3/userinfo',
    ARRAY['openid', 'email', 'profile'],
    'https://developers.google.com/identity/images/g-logo.png',
    'https://developers.google.com/identity/protocols/oauth2'
  )
ON CONFLICT (provider) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  auth_url = EXCLUDED.auth_url,
  token_url = EXCLUDED.token_url,
  userinfo_url = EXCLUDED.userinfo_url,
  default_scopes = EXCLUDED.default_scopes,
  icon_url = EXCLUDED.icon_url,
  documentation_url = EXCLUDED.documentation_url,
  updated_at = NOW();
