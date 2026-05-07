-- ═══════════════════════════════════════════════════════════
-- L2 Data REAL: per-workspace SQL schemas (multi-tenant isolation)
-- Each workspace has its own schema → users execute SQL in their schema only
-- ═══════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS ws_holdings;
CREATE SCHEMA IF NOT EXISTS ws_anima;
CREATE SCHEMA IF NOT EXISTS ws_zeniipo;
CREATE SCHEMA IF NOT EXISTS ws_digital;
CREATE SCHEMA IF NOT EXISTS ws_wellkoc;
CREATE SCHEMA IF NOT EXISTS ws_nexbuild;
CREATE SCHEMA IF NOT EXISTS ws_bthome;
CREATE SCHEMA IF NOT EXISTS ws_capital;

-- Grant app user privileges on each schema
DO $$
DECLARE ws TEXT;
BEGIN
  FOREACH ws IN ARRAY ARRAY['holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital'] LOOP
    EXECUTE format('GRANT USAGE, CREATE ON SCHEMA ws_%I TO zeni_app', ws);
    EXECUTE format('GRANT ALL ON ALL TABLES IN SCHEMA ws_%I TO zeni_app', ws);
    EXECUTE format('GRANT ALL ON ALL SEQUENCES IN SCHEMA ws_%I TO zeni_app', ws);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA ws_%I GRANT ALL ON TABLES TO zeni_app', ws);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA ws_%I GRANT ALL ON SEQUENCES TO zeni_app', ws);
  END LOOP;
END $$;

-- Sample starter table in each workspace (key-value store demo)
DO $$
DECLARE ws TEXT;
BEGIN
  FOREACH ws IN ARRAY ARRAY['holdings','anima','zeniipo','digital','wellkoc','nexbuild','bthome','capital'] LOOP
    EXECUTE format('
      CREATE TABLE IF NOT EXISTS ws_%I.kv (
        key VARCHAR(255) PRIMARY KEY,
        value TEXT,
        meta JSONB DEFAULT ''{}''::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )', ws);
    EXECUTE format('
      CREATE TABLE IF NOT EXISTS ws_%I.events (
        id BIGSERIAL PRIMARY KEY,
        kind VARCHAR(64) NOT NULL,
        payload JSONB NOT NULL,
        actor VARCHAR(255),
        ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
      )', ws);
  END LOOP;
END $$;

-- Seed a couple of demo rows for ws_anima so empty queries return something
INSERT INTO ws_anima.kv (key, value) VALUES
  ('greeting', 'Xin chào ANIMA Care'),
  ('app_version', 'v1.0.0'),
  ('feature_flag.affiliate', 'true')
ON CONFLICT (key) DO NOTHING;

INSERT INTO ws_anima.events (kind, payload, actor) VALUES
  ('order.created', '{"sku":"anima-119","qty":1,"vnd":890000}'::jsonb, 'demo@anima.global'),
  ('order.paid',    '{"sku":"anima-119","method":"vnpay"}'::jsonb,     'demo@anima.global')
ON CONFLICT DO NOTHING;
