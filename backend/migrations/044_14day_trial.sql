-- Migration 044 — 14-day Free Trial enforcement
-- New workspaces get 14 days of Free tier, then must subscribe to continue

-- 1. Add trial_ends_at to workspaces
ALTER TABLE workspaces
  ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trial_status VARCHAR(20) DEFAULT 'active';
  -- trial_status: 'active' | 'expired' | 'converted' (subscribed) | 'extended'

-- 2. Backfill existing workspaces — give them trial = NOW() + 14 days from creation
-- (workspaces created earlier than 14 days ago → trial_ends_at = NOW() + 7 days grace)
UPDATE workspaces
SET trial_ends_at = COALESCE(
  trial_ends_at,
  CASE
    WHEN created_at IS NULL OR created_at < NOW() - INTERVAL '14 days'
      THEN NOW() + INTERVAL '7 days'  -- grace period for old accounts
    ELSE created_at + INTERVAL '14 days'
  END
)
WHERE trial_ends_at IS NULL;

-- 3. Auto-extend trial for special accounts (super-admin)
UPDATE workspaces
SET trial_ends_at = NOW() + INTERVAL '10 years',
    trial_status = 'extended'
WHERE id IN ('nexbuild', 'nexbuild_holdings');

-- 4. Diagnostic
DO $$
DECLARE r RECORD; total INT; expired INT;
BEGIN
  SELECT COUNT(*) INTO total FROM workspaces;
  SELECT COUNT(*) INTO expired FROM workspaces WHERE trial_ends_at < NOW();
  RAISE NOTICE 'Trial migration: total=% expired=%', total, expired;
  FOR r IN SELECT id, name, trial_ends_at, trial_status FROM workspaces ORDER BY created_at DESC LIMIT 10 LOOP
    RAISE NOTICE '  ws=% (% trial_ends=% status=%)', r.id, r.name, r.trial_ends_at, r.trial_status;
  END LOOP;
END$$;

-- 5. Index for trial status checks
CREATE INDEX IF NOT EXISTS idx_workspaces_trial ON workspaces(trial_ends_at, trial_status) WHERE trial_status = 'active';
