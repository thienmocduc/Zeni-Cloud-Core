-- Migration 042 — CLEANUP TEST/DEMO WORKSPACES (keep only nexbuild — the live project)
-- Removes all workspaces created during multi-tenant testing phase

-- List of workspaces to delete (everything except nexbuild)
-- doanhnhancaotuan_gmail_com (Tuan Cao's Workspace)
-- test_studio
-- cong_ty_zeni_digital
-- testco1777598452
-- iso1777608183
-- debug1777630844

-- Wipe dependent rows first (router_*, billing, audit, etc.)
DELETE FROM router_usage_log         WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM router_tenant_quotas     WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM router_cache             WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM workspace_subscriptions  WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM workspace_usage          WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM quota_events             WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM admin_access_requests    WHERE customer_workspace_id NOT IN ('nexbuild');
DELETE FROM privacy_preferences      WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM output_filter_logs       WHERE workspace_id NOT IN ('nexbuild');
DELETE FROM audit_log                WHERE workspace_id NOT IN ('nexbuild');

-- Now wipe workspaces (CASCADE handles remaining FK deps from Sprint A4-A7 tables)
DELETE FROM workspaces WHERE id NOT IN ('nexbuild');

-- Demote any user with role='Owner' EXCEPT system super-admin
UPDATE users
SET role = 'Admin'
WHERE role = 'Owner'
  AND lower(email) <> 'caotuanphat581@gmail.com';
