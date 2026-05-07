-- ════════════════════════════════════════════════════════════════════
-- FIX 999 — Restore user_workspaces link cho chairman@clawwits.com
-- 07/05/2026 — Idempotent + introspect schema
-- ════════════════════════════════════════════════════════════════════

-- 0. Introspect workspaces schema
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'workspaces'
ORDER BY ordinal_position;

DO $$
DECLARE
  ws_exists INT;
  user_id_val UUID;
  link_count INT;
  has_code_col BOOLEAN;
  has_owner_col BOOLEAN;
BEGIN
  -- Check schema
  SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'workspaces' AND column_name = 'code') INTO has_code_col;
  SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'workspaces' AND column_name = 'owner_user_id') INTO has_owner_col;
  RAISE NOTICE 'workspaces.code exists: %, workspaces.owner_user_id exists: %', has_code_col, has_owner_col;

  -- Find clawwits user
  SELECT id INTO user_id_val FROM users WHERE email = 'chairman@clawwits.com';
  IF user_id_val IS NULL THEN
    RAISE NOTICE 'ERROR: chairman@clawwits.com NOT FOUND';
    RETURN;
  END IF;
  RAISE NOTICE 'clawwits user_id=%', user_id_val;

  -- Check workspace
  SELECT COUNT(*) INTO ws_exists FROM workspaces WHERE id = 'clawwits_flatform';
  RAISE NOTICE 'workspace clawwits_flatform exists: %', ws_exists;

  IF ws_exists = 0 THEN
    -- Create with all required fields (use dynamic SQL to handle schema variations)
    IF has_code_col AND has_owner_col THEN
      EXECUTE 'INSERT INTO workspaces (id, code, name, owner_user_id) VALUES ($1, $2, $3, $4)'
        USING 'clawwits_flatform', 'CLAWWITS', 'ClawWits Platform', user_id_val;
    ELSIF has_code_col THEN
      EXECUTE 'INSERT INTO workspaces (id, code, name) VALUES ($1, $2, $3)'
        USING 'clawwits_flatform', 'CLAWWITS', 'ClawWits Platform';
    ELSIF has_owner_col THEN
      EXECUTE 'INSERT INTO workspaces (id, name, owner_user_id) VALUES ($1, $2, $3)'
        USING 'clawwits_flatform', 'ClawWits Platform', user_id_val;
    ELSE
      EXECUTE 'INSERT INTO workspaces (id, name) VALUES ($1, $2)'
        USING 'clawwits_flatform', 'ClawWits Platform';
    END IF;
    RAISE NOTICE '✓ created workspace clawwits_flatform';
  END IF;

  -- Link user → workspace
  INSERT INTO user_workspaces (user_id, workspace_id, role)
  VALUES (user_id_val, 'clawwits_flatform', 'Owner')
  ON CONFLICT (user_id, workspace_id) DO UPDATE SET role = 'Owner';

  SELECT COUNT(*) INTO link_count
    FROM user_workspaces
    WHERE user_id = user_id_val AND workspace_id = 'clawwits_flatform';
  RAISE NOTICE '✓ user_workspaces link count: % (expect 1)', link_count;
END$$;

-- Verify final state
SELECT u.email, uw.role, uw.workspace_id, w.name AS workspace_name
FROM users u
JOIN user_workspaces uw ON uw.user_id = u.id
LEFT JOIN workspaces w ON w.id = uw.workspace_id
WHERE u.email = 'chairman@clawwits.com';
