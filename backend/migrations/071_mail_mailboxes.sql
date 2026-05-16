-- L7 Mail · Migration 071: mail_mailboxes table
-- Mỗi domain có nhiều mailbox (hello@, info@, support@, ...)
-- Plan limit enforce ở API layer (Starter 5, Pro 20, Business unlimited).

CREATE TABLE IF NOT EXISTS mail_mailboxes (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id       UUID         NOT NULL REFERENCES mail_domains(id) ON DELETE CASCADE,
    username        VARCHAR(64)  NOT NULL,                                -- 'hello' cho hello@vietcontech.com
    password_hash   TEXT         NOT NULL,                                -- bcrypt
    display_name    VARCHAR(128),
    quota_mb        INT          NOT NULL DEFAULT 5120,                   -- 5GB default
    used_mb         INT          NOT NULL DEFAULT 0,
    is_catchall     BOOLEAN      NOT NULL DEFAULT FALSE,                  -- catchall mailbox = *@domain
    aliases         TEXT[],                                                -- VD: ARRAY['info','support']
    forward_to      TEXT,                                                  -- forward tất cả tới external email
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (domain_id, username)
);

CREATE INDEX IF NOT EXISTS idx_mail_mailboxes_domain     ON mail_mailboxes(domain_id);
CREATE INDEX IF NOT EXISTS idx_mail_mailboxes_active     ON mail_mailboxes(is_active) WHERE is_active = TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_mailboxes_catchall
    ON mail_mailboxes(domain_id) WHERE is_catchall = TRUE;                -- max 1 catchall per domain

COMMENT ON TABLE  mail_mailboxes IS 'L7 Mail · individual mailboxes per domain';
COMMENT ON COLUMN mail_mailboxes.aliases IS 'Additional usernames map to same mailbox (info@, support@)';
COMMENT ON COLUMN mail_mailboxes.forward_to IS 'External email to forward all incoming mail';
