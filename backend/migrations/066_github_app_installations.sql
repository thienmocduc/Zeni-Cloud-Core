-- Migration 066 — GitHub App installations (Phase 1 P1.1)
-- ════════════════════════════════════════════════════════════════════
-- v169+ Phase 1 chairman approved 2026-05-11 — fully automated GitHub import.
-- KHÔNG đụng github_connections cũ (webhook secret manual flow vẫn còn).
-- Bảng MỚI track GitHub App installations per workspace.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS github_app_installations (
    installation_id    VARCHAR(64) PRIMARY KEY,                -- GitHub installation ID
    workspace_id       VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    installed_by       VARCHAR(255) NOT NULL,                  -- user email
    github_account     VARCHAR(128),                            -- GitHub org/user name
    metadata           JSONB DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    revoked_at         TIMESTAMPTZ                              -- NULL = active
);

CREATE INDEX IF NOT EXISTS idx_gh_app_install_ws ON github_app_installations(workspace_id);
CREATE INDEX IF NOT EXISTS idx_gh_app_install_active ON github_app_installations(workspace_id) WHERE revoked_at IS NULL;

COMMENT ON TABLE github_app_installations IS
    'Phase 1 P1.1 — GitHub App installations per workspace. Customer click Import từ GitHub → install Zeni Cloud App 1 lần → mọi repo auto-deploy. KHÔNG đụng github_connections cũ.';
