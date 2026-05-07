-- ═══════════════════════════════════════════════════════════
-- 019: Auth Extensions — Email Verification, Phone OTP,
--      Login Challenges (2FA gate), users.phone/email_verified
--
-- Notes:
--   - users.id is UUID (PGUUID), so all user_id FKs are UUID.
--   - Idempotent: CREATE IF NOT EXISTS, ALTER … IF NOT EXISTS.
--   - All timestamps TIMESTAMPTZ (UTC).
--   - bcrypt-hashed OTP codes only — never plaintext.
--   - Grants extended to zeni_app (matches earlier migration style).
-- ═══════════════════════════════════════════════════════════

-- ── 1. Email verification tokens (one-time, 24h expiry) ────
CREATE TABLE IF NOT EXISTS email_verifications (
    id            BIGSERIAL    PRIMARY KEY,
    user_id       UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token         TEXT         UNIQUE NOT NULL,
    email         TEXT         NOT NULL,
    expires_at    TIMESTAMPTZ  NOT NULL,
    verified_at   TIMESTAMPTZ,
    sent_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    attempts      INT          NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_email_verif_token ON email_verifications(token);
CREATE INDEX IF NOT EXISTS idx_email_verif_user  ON email_verifications(user_id);
CREATE INDEX IF NOT EXISTS idx_email_verif_exp   ON email_verifications(expires_at);

-- ── 2. Phone OTP codes (6-digit, bcrypt-hashed, 10min) ─────
CREATE TABLE IF NOT EXISTS phone_otps (
    id            BIGSERIAL    PRIMARY KEY,
    phone         TEXT         NOT NULL,
    code_hash     TEXT         NOT NULL,
    purpose       TEXT         NOT NULL CHECK (purpose IN ('signup','login','reset','add_phone','step_up')),
    user_id       UUID         REFERENCES users(id) ON DELETE CASCADE,
    expires_at    TIMESTAMPTZ  NOT NULL,
    verified_at   TIMESTAMPTZ,
    sent_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    attempts      INT          NOT NULL DEFAULT 0,
    ip            TEXT
);
CREATE INDEX IF NOT EXISTS idx_phone_otps_phone ON phone_otps(phone, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_phone_otps_user  ON phone_otps(user_id);
CREATE INDEX IF NOT EXISTS idx_phone_otps_exp   ON phone_otps(expires_at);

-- ── 3. Add columns to users (idempotent) ───────────────────
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS email_verified_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS phone              TEXT,
  ADD COLUMN IF NOT EXISTS phone_verified_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS mfa_required       BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone
  ON users(phone) WHERE phone IS NOT NULL;

-- ── 4. Pending login challenges (2FA gate) ─────────────────
CREATE TABLE IF NOT EXISTS login_challenges (
    id                BIGSERIAL    PRIMARY KEY,
    challenge_token   TEXT         UNIQUE NOT NULL,
    user_id           UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    method            TEXT         NOT NULL CHECK (method IN ('totp','sms_otp','email_otp')),
    expires_at        TIMESTAMPTZ  NOT NULL,
    consumed          BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_login_challenges_token ON login_challenges(challenge_token);
CREATE INDEX IF NOT EXISTS idx_login_challenges_exp   ON login_challenges(expires_at);

-- ── 5. Grants (idempotent) ──────────────────────────────────
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zeni_app') THEN
    EXECUTE 'GRANT ALL PRIVILEGES ON email_verifications TO zeni_app';
    EXECUTE 'GRANT ALL PRIVILEGES ON phone_otps          TO zeni_app';
    EXECUTE 'GRANT ALL PRIVILEGES ON login_challenges    TO zeni_app';
    EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE email_verifications_id_seq TO zeni_app';
    EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE phone_otps_id_seq          TO zeni_app';
    EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE login_challenges_id_seq    TO zeni_app';
  END IF;
END$$;
