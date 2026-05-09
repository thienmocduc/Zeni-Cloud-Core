-- Reset password vietcontech.com@gmail.com → Vietcontech@123
-- Idempotent: UPDATE only

UPDATE users
SET password_hash = '$2b$12$4nJy6kEWz.2tZD1pJUDZUOIUJvHAZngL7jRo.EEbEnpmePNWNSRRS', disabled = FALSE
WHERE email = 'vietcontech.com@gmail.com';

-- Verify
SELECT email, role, disabled, last_login FROM users WHERE email = 'vietcontech.com@gmail.com';
