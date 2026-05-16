-- L7 Mail · Migration 070: mail_domains table
-- Mỗi workspace có thể đăng ký nhiều domain (vd vietcontech.com, nexbuild.holdings)
-- DKIM keypair tạo per-domain, lưu encrypted qua Vault (encrypt() function).

CREATE TABLE IF NOT EXISTS mail_domains (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     VARCHAR(64)  NOT NULL,
    domain           VARCHAR(255) NOT NULL,
    status           VARCHAR(32)  NOT NULL DEFAULT 'pending_dns',  -- pending_dns | active | suspended
    dkim_selector    VARCHAR(32)  NOT NULL DEFAULT 'zeni',
    dkim_private_key TEXT         NOT NULL,                        -- encrypted via app.core.vault.encrypt()
    dkim_public_key  TEXT         NOT NULL,                        -- public key (raw, DNS record-ready)
    spf_verified     BOOLEAN      NOT NULL DEFAULT FALSE,
    mx_verified      BOOLEAN      NOT NULL DEFAULT FALSE,
    dkim_verified    BOOLEAN      NOT NULL DEFAULT FALSE,
    dmarc_policy     VARCHAR(16)  NOT NULL DEFAULT 'quarantine',   -- none | quarantine | reject
    plan             VARCHAR(32)  NOT NULL DEFAULT 'starter',      -- starter | pro | business | enterprise
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (domain)
);

CREATE INDEX IF NOT EXISTS idx_mail_domains_ws        ON mail_domains(workspace_id);
CREATE INDEX IF NOT EXISTS idx_mail_domains_status    ON mail_domains(status);
CREATE INDEX IF NOT EXISTS idx_mail_domains_created   ON mail_domains(created_at DESC);

COMMENT ON TABLE  mail_domains IS 'L7 Mail · domains đăng ký cho mail hosting per-workspace';
COMMENT ON COLUMN mail_domains.status IS 'Lifecycle: pending_dns → active (sau khi verify DNS) → suspended';
COMMENT ON COLUMN mail_domains.plan IS 'Starter $2 / Pro $5 / Business $10 / Enterprise $25 per month';
