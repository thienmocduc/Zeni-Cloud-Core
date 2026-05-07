-- ═══════════════════════════════════════════════════════════
-- Marketing landing waitlist: capture leads from zenicloud.io/
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS waitlist (
  id           BIGSERIAL    PRIMARY KEY,
  email        VARCHAR(255) UNIQUE NOT NULL,
  source       VARCHAR(32)  NOT NULL DEFAULT 'landing',
  lang         VARCHAR(8),
  referrer     VARCHAR(512),
  user_agent   VARCHAR(512),
  ip_hint      VARCHAR(64),       -- coarse, not full IP (privacy)
  contacted_at TIMESTAMPTZ,        -- when we sent invite
  invited      BOOLEAN      NOT NULL DEFAULT FALSE,
  notes        TEXT,
  created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_waitlist_created ON waitlist(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_waitlist_email   ON waitlist(email);

GRANT ALL PRIVILEGES ON waitlist TO zeni_app;
GRANT USAGE, SELECT ON waitlist_id_seq TO zeni_app;
