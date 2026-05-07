-- Migration 031 — RESET DEMO DATA (multi-tenant isolation hotfix) — explicit DELETEs

-- Wipe dependent rows first (no DO block to surface errors clearly)
DELETE FROM router_usage_log         WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM router_tenant_quotas     WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM router_cache             WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM workspace_subscriptions  WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM workspace_usage          WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM quota_events             WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM admin_access_requests    WHERE customer_workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM privacy_preferences      WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM output_filter_logs       WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');
DELETE FROM audit_log                WHERE workspace_id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');

-- Now wipe workspaces (CASCADE handles remaining FK deps from Sprint A4-A5 tables)
DELETE FROM workspaces WHERE id IN ('holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital');

-- Demote any user with role='Owner' EXCEPT system super-admin
UPDATE users
SET role = 'Admin'
WHERE role = 'Owner'
  AND lower(email) <> 'caotuanphat581@gmail.com';
