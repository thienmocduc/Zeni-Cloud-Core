-- ═══════════════════════════════════════════════════════════
-- GRANT privileges on zeni_cloud DB to zeni_app (app user)
-- Run after 001_init.sql, as postgres root
-- ═══════════════════════════════════════════════════════════

GRANT CONNECT ON DATABASE zeni_cloud TO zeni_app;
GRANT USAGE, CREATE ON SCHEMA public TO zeni_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO zeni_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO zeni_app;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO zeni_app;

-- Future tables created by app (via Alembic migrations later) auto-grant too
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO zeni_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO zeni_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO zeni_app;
