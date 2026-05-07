-- ═══════════════════════════════════════════════════════════
-- Fix workspace codes too long (8+ chars overflow badge UI)
-- For workspaces auto-created from OAuth before code generator fix
-- ═══════════════════════════════════════════════════════════

-- Show current bad codes
\echo '--- Workspaces with long codes (>4 chars) before fix ---'
SELECT id, code, name FROM workspaces WHERE LENGTH(code) > 4 OR code LIKE '%\_%' ESCAPE '\';

-- Fix specific known bad ones
UPDATE workspaces SET code = 'DOA' WHERE id = 'doanhnhancaotuan_gmail_com' OR code = 'DOANHNHA';
UPDATE workspaces SET code = 'TST' WHERE id LIKE 'test_studio%' OR code LIKE 'TEST_STU%';

-- Generic fix: any workspace where code > 4 chars, take first 3 alpha chars
UPDATE workspaces
SET code = UPPER(LEFT(REGEXP_REPLACE(code, '[^A-Za-z]', '', 'g'), 3))
WHERE LENGTH(code) > 4;

-- Final state
\echo '--- All workspaces after fix ---'
SELECT id, code, name FROM workspaces ORDER BY id;
