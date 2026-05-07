-- ═══════════════════════════════════════════════════════════
-- Update admin email from placeholder to real CEO email
-- ═══════════════════════════════════════════════════════════

UPDATE users
SET email = 'caotuanphat581@gmail.com'
WHERE email = 'ceo@zeni-holdings.vn';
