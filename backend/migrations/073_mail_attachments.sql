-- L7 Mail · Migration 073: mail_attachments table
-- Attachments stored on GCS (gs://zeni-mail-attachments/{ws}/{message_id}/{filename})
-- Metadata kept in Postgres for quick listing without GCS roundtrip.

CREATE TABLE IF NOT EXISTS mail_attachments (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      UUID          NOT NULL REFERENCES mail_messages(id) ON DELETE CASCADE,
    filename        VARCHAR(512),
    content_type    VARCHAR(128),
    size_bytes      BIGINT        NOT NULL DEFAULT 0,
    gcs_path        TEXT          NOT NULL,                            -- gs://...
    sha256          VARCHAR(64),                                        -- for dedup + integrity check
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mail_attachments_msg     ON mail_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_mail_attachments_sha256  ON mail_attachments(sha256) WHERE sha256 IS NOT NULL;

COMMENT ON TABLE  mail_attachments IS 'L7 Mail · attachment metadata (files on GCS bucket zeni-mail-attachments)';
COMMENT ON COLUMN mail_attachments.gcs_path IS 'Format: gs://zeni-mail-attachments/{ws}/{msg_id}/{filename}';
