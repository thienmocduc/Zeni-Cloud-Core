-- L7 Mail · Migration 072: mail_messages table
-- Parsed MIME stored per row, raw .eml stored on GCS (raw_mime_gcs path).
-- Body HTML/Text both kept to avoid re-parse on display.

CREATE TABLE IF NOT EXISTS mail_messages (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id      UUID          NOT NULL REFERENCES mail_mailboxes(id) ON DELETE CASCADE,
    folder          VARCHAR(32)   NOT NULL DEFAULT 'inbox',                -- inbox|sent|drafts|trash|<custom>
    message_id      VARCHAR(512)  UNIQUE,                                  -- RFC 5322 Message-ID
    thread_id       VARCHAR(64),                                            -- conversation grouping (Gmail-style)
    from_addr       VARCHAR(255)  NOT NULL,
    to_addrs        TEXT[]        NOT NULL,                                 -- ARRAY of TO addresses
    cc_addrs        TEXT[],
    bcc_addrs       TEXT[],
    subject         TEXT,
    body_text       TEXT,                                                   -- plain text body
    body_html       TEXT,                                                   -- HTML body (sanitized via DOMPurify on render)
    headers         JSONB         NOT NULL DEFAULT '{}'::jsonb,             -- raw headers parsed
    raw_mime_gcs    TEXT,                                                   -- gs://zeni-mail-raw/{ws}/{id}.eml
    is_read         BOOLEAN       NOT NULL DEFAULT FALSE,
    is_starred      BOOLEAN       NOT NULL DEFAULT FALSE,
    spam_score      FLOAT,                                                  -- rspamd score; > 7.0 = spam
    received_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),                   -- timestamp khi server nhận
    sent_at         TIMESTAMPTZ                                             -- timestamp khi user click Send (sent folder)
);

CREATE INDEX IF NOT EXISTS idx_mail_messages_mailbox_folder
    ON mail_messages(mailbox_id, folder, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_mail_messages_thread
    ON mail_messages(thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mail_messages_unread
    ON mail_messages(mailbox_id, folder) WHERE is_read = FALSE;
CREATE INDEX IF NOT EXISTS idx_mail_messages_starred
    ON mail_messages(mailbox_id) WHERE is_starred = TRUE;
CREATE INDEX IF NOT EXISTS idx_mail_messages_search
    ON mail_messages USING gin(to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(body_text, '')));

COMMENT ON TABLE  mail_messages IS 'L7 Mail · parsed email messages (raw MIME on GCS)';
COMMENT ON COLUMN mail_messages.spam_score IS 'rspamd anti-spam score; >7.0 means spam (rejected upstream)';
