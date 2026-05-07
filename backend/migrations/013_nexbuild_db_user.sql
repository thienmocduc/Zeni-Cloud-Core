-- ═══════════════════════════════════════════════════════════
-- NexBuild dedicated DB user with isolated access to ws_nexbuild
-- Cho phép NexBuild backend connect direct PostgreSQL (no HTTP overhead)
-- ═══════════════════════════════════════════════════════════

-- Step 1: Create user (chairman set password manually after this script)
-- Password sẽ được set bằng `gcloud sql users set-password`
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexbuild_app') THEN
    CREATE USER nexbuild_app WITH PASSWORD 'PLACEHOLDER_WILL_BE_RESET_VIA_GCLOUD';
  END IF;
END $$;

-- Step 2: Grant access to ws_nexbuild only
GRANT CONNECT ON DATABASE zeni_cloud TO nexbuild_app;
GRANT USAGE, CREATE ON SCHEMA ws_nexbuild TO nexbuild_app;
GRANT ALL PRIVILEGES ON ALL TABLES    IN SCHEMA ws_nexbuild TO nexbuild_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA ws_nexbuild TO nexbuild_app;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA ws_nexbuild TO nexbuild_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ws_nexbuild GRANT ALL ON TABLES TO nexbuild_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ws_nexbuild GRANT ALL ON SEQUENCES TO nexbuild_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ws_nexbuild GRANT EXECUTE ON FUNCTIONS TO nexbuild_app;

-- Step 3: Set search_path so SQL like `SELECT * FROM users` resolves to ws_nexbuild.users
ALTER USER nexbuild_app SET search_path = ws_nexbuild, public;

-- Step 4: REVOKE cross-schema access (multi-tenant isolation)
DO $$
DECLARE
  other_ws TEXT;
BEGIN
  FOREACH other_ws IN ARRAY ARRAY['holdings','anima','zeniipo','digital','wellkoc','bthome','capital'] LOOP
    EXECUTE format('REVOKE ALL ON SCHEMA ws_%I FROM nexbuild_app', other_ws);
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA ws_%I FROM nexbuild_app', other_ws);
  END LOOP;
END $$;

-- Step 5: Limit query duration (prevent runaway queries)
ALTER USER nexbuild_app SET statement_timeout = '60s';
ALTER USER nexbuild_app SET idle_in_transaction_session_timeout = '5min';
