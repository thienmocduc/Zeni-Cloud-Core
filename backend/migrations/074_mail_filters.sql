-- L7 Mail · Migration 074: mail_filters table
-- Per-mailbox rules để auto-route mail vào folder/star/delete.
-- Conditions + actions stored as JSONB cho flexibility.

CREATE TABLE IF NOT EXISTS mail_filters (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id      UUID         NOT NULL REFERENCES mail_mailboxes(id) ON DELETE CASCADE,
    name            VARCHAR(128),
    priority        INT          NOT NULL DEFAULT 100,                      -- lower runs first
    conditions      JSONB        NOT NULL,                                  -- vd: {"from":"*@spam.com"} hoặc {"subject_contains":"newsletter"}
    actions         JSONB        NOT NULL,                                  -- vd: {"move_to":"trash"} hoặc {"star":true,"forward_to":"info@..."}
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mail_filters_mailbox   ON mail_filters(mailbox_id, priority);
CREATE INDEX IF NOT EXISTS idx_mail_filters_enabled   ON mail_filters(mailbox_id) WHERE enabled = TRUE;

COMMENT ON TABLE  mail_filters IS 'L7 Mail · per-mailbox auto-routing rules';
COMMENT ON COLUMN mail_filters.priority IS 'Lower number = higher priority (runs first)';
COMMENT ON COLUMN mail_filters.conditions IS 'JSON: {from, to, subject_contains, body_contains, has_attachment, ...}';
COMMENT ON COLUMN mail_filters.actions    IS 'JSON: {move_to, star, mark_read, forward_to, delete}';
