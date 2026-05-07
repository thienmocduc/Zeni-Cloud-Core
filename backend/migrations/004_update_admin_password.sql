-- ═══════════════════════════════════════════════════════════
-- Update admin password to CEO's chosen password
-- Password: Tuan6768@$ (bcrypt cost 12)
-- ═══════════════════════════════════════════════════════════

UPDATE users
SET password_hash = '$2b$12$GfWnbJCdlTjlDL3XXTc7UuH2amhM4ZlEZH.7op5jMHE8uzLz94L7G',
    last_login = NULL
WHERE email = 'caotuanphat581@gmail.com';
