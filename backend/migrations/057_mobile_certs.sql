-- Migration 057 — Mobile Cert Manager (P0#2 ClawWits)
-- Apple Developer cert + Android keystore + provisioning profile + APNs .p8 key
-- Auto-renew alert + integrate with Identity Vault for encryption

CREATE TABLE IF NOT EXISTS mobile_certs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  -- Identity
  name VARCHAR(120) NOT NULL,                  -- "ClawWits Production iOS Distribution"
  cert_type VARCHAR(40) NOT NULL,              -- ios_distribution | ios_development | apns_p8 | android_upload | android_signing | provisioning_profile
  platform VARCHAR(20) NOT NULL,               -- ios | android
  -- Binary content (encrypted via Vault)
  vault_secret_id VARCHAR(120) NOT NULL,       -- ref to identity_secrets table
  cert_password_secret_id VARCHAR(120),        -- separate secret for .p12 password
  -- Apple-specific
  apple_team_id VARCHAR(20),
  apple_bundle_id VARCHAR(180),                -- com.clawwits.app
  apple_key_id VARCHAR(20),                    -- for .p8 APNs auth key
  -- Android-specific
  android_package_name VARCHAR(180),           -- com.clawwits.app
  keystore_alias VARCHAR(120),
  -- Provisioning
  provisioning_uuid VARCHAR(60),
  provisioning_devices JSONB DEFAULT '[]'::jsonb,
  -- Validity
  issued_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  serial_number VARCHAR(100),
  -- Alerts
  alert_30d_sent BOOLEAN DEFAULT FALSE,
  alert_7d_sent BOOLEAN DEFAULT FALSE,
  alert_1d_sent BOOLEAN DEFAULT FALSE,
  -- Audit
  uploaded_by UUID,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mobile_certs_ws ON mobile_certs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_mobile_certs_expires ON mobile_certs(expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS mobile_cert_audit (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cert_id UUID NOT NULL REFERENCES mobile_certs(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  action VARCHAR(40),                          -- upload | rotate | delete | renew | access
  performed_by UUID,
  details JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cert_audit_cert ON mobile_cert_audit(cert_id, created_at DESC);

COMMENT ON TABLE mobile_certs IS 'Apple/Android mobile cert vault: APNs .p8, .p12 dist cert, Android keystore';
COMMENT ON TABLE mobile_cert_audit IS 'Cert access audit log for compliance';
