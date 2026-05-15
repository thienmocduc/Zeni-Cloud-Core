-- Backfill audit_log.workspace_id từ NULL → workspace dựa trên actor email
-- Idempotent — chỉ touch rows có workspace_id IS NULL

DO $$
DECLARE
  total_null INT;
  total_backfilled INT;
BEGIN
  SELECT COUNT(*) INTO total_null FROM audit_log WHERE workspace_id IS NULL;
  RAISE NOTICE 'BEFORE: audit_log rows with NULL workspace_id = %', total_null;
END$$;

-- Backfill: cho mỗi audit_log row có actor email + workspace_id NULL,
-- set workspace_id = first workspace của user đó (nếu có)
UPDATE audit_log al
SET workspace_id = sub.workspace_id
FROM (
  SELECT u.email, MIN(uw.workspace_id) AS workspace_id
  FROM users u
  JOIN user_workspaces uw ON uw.user_id = u.id
  GROUP BY u.email
) sub
WHERE al.workspace_id IS NULL
  AND al.actor = sub.email;

DO $$
DECLARE
  remaining_null INT;
  total INT;
BEGIN
  SELECT COUNT(*) INTO remaining_null FROM audit_log WHERE workspace_id IS NULL;
  SELECT COUNT(*) INTO total FROM audit_log;
  RAISE NOTICE 'AFTER: % rows still NULL out of % total (%.1f%% backfilled)',
    remaining_null, total, ((total - remaining_null) * 100.0 / NULLIF(total, 0));
END$$;

-- Verify by sample
SELECT workspace_id, COUNT(*) FROM audit_log GROUP BY workspace_id ORDER BY COUNT(*) DESC LIMIT 10;
