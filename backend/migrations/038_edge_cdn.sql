-- ============================================================================
-- Migration 038 — Edge CDN + Custom Domain Management (Sprint A7)
--
-- Purpose: Edge layer + multi-domain + SSL automation cho customer apps
--   được deploy trên Zeni Cloud Compute (Cloud Run / VM / Container).
--
-- Stack:
--   * Cloudflare API (chính): Workers, WAF, R2, Universal SSL
--   * Cloud CDN (fallback): cho khách enterprise muốn tách
--   * Fastly (option): tương lai
--
-- Tables (6):
--   1. cdn_zones             — CDN config per domain
--   2. cdn_routes            — path-based routing/caching rules
--   3. cdn_cache_purge_log   — audit purge history
--   4. cdn_security_rules    — WAF / rate limit / IP block / country block
--   5. cdn_analytics_daily   — daily aggregates (requests, bandwidth, cache)
--   6. cdn_certificates      — SSL/TLS cert lifecycle (Let's Encrypt + custom)
--
-- An toàn:
--   * Mọi endpoint /edge/* yêu cầu workspace access + (PAT scope 'edge'|'full')
--   * audit_push cho mọi sensitive action (purge, security_rule, cert_issue)
--   * Cert key_pem chứa secret_ref → trỏ vào Secret Manager (không lưu plain)
-- ============================================================================

-- ─── 1. CDN Zones ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cdn_zones (
    id                  BIGSERIAL PRIMARY KEY,
    workspace_id        VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id          UUID REFERENCES projects(id) ON DELETE SET NULL,
    domain              VARCHAR(253) NOT NULL,
    cdn_provider        VARCHAR(24) NOT NULL DEFAULT 'cloudflare'
                        CHECK (cdn_provider IN ('cloudflare','fastly','cloud_cdn')),
    zone_id             VARCHAR(120),                        -- provider zone id (CF zone id, etc.)
    origin_url          VARCHAR(512),                         -- backend origin (e.g. https://abc.run.app)
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','provisioning','active','disabled','error')),
    ssl_status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (ssl_status IN ('pending','active','expired','error','none')),
    ssl_provider        VARCHAR(24) DEFAULT 'cloudflare_universal'
                        CHECK (ssl_provider IN ('lets_encrypt','custom','cloudflare_universal','google_managed')),
    ssl_expires_at      TIMESTAMPTZ,
    http2_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    http3_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    waf_enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    bot_protection      BOOLEAN NOT NULL DEFAULT TRUE,
    always_use_https    BOOLEAN NOT NULL DEFAULT TRUE,
    min_tls_version     VARCHAR(8) DEFAULT '1.2',
    metadata            JSONB DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, domain)
);
CREATE INDEX IF NOT EXISTS idx_cdn_zones_ws        ON cdn_zones(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cdn_zones_project   ON cdn_zones(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cdn_zones_domain    ON cdn_zones(domain);
CREATE INDEX IF NOT EXISTS idx_cdn_zones_status    ON cdn_zones(status, ssl_status);
CREATE INDEX IF NOT EXISTS idx_cdn_zones_ssl_exp   ON cdn_zones(ssl_expires_at) WHERE ssl_expires_at IS NOT NULL;


-- ─── 2. CDN Routes (path-based routing & caching) ───────────────────────────
CREATE TABLE IF NOT EXISTS cdn_routes (
    id                    BIGSERIAL PRIMARY KEY,
    zone_id               BIGINT NOT NULL REFERENCES cdn_zones(id) ON DELETE CASCADE,
    path_pattern          VARCHAR(512) NOT NULL,             -- e.g. '/api/*', '/static/*', '/'
    origin_url            VARCHAR(512),                       -- override origin per route
    cache_ttl_seconds     INT NOT NULL DEFAULT 0,             -- edge cache TTL (0 = no edge cache)
    cache_browser_ttl     INT NOT NULL DEFAULT 0,             -- Cache-Control max-age cho browser
    bypass_cache_cookie   VARCHAR(120),                       -- cookie name -> bypass cache when present
    cache_key_query_strings  TEXT[] DEFAULT ARRAY[]::TEXT[],  -- query keys included in cache key
    redirect_to           VARCHAR(512),                       -- nếu có thì 301/302 redirect
    redirect_status       INT DEFAULT 302
                          CHECK (redirect_status IN (301,302,307,308)),
    methods               TEXT[] DEFAULT ARRAY['GET','HEAD']::TEXT[],
    headers_add           JSONB DEFAULT '{}'::JSONB,          -- header injection { "X-Frame-Options": "DENY" }
    headers_remove        TEXT[] DEFAULT ARRAY[]::TEXT[],
    priority              INT NOT NULL DEFAULT 100,           -- thấp = ưu tiên hơn
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cdn_routes_zone      ON cdn_routes(zone_id, priority);
CREATE INDEX IF NOT EXISTS idx_cdn_routes_pattern   ON cdn_routes(zone_id, path_pattern);


-- ─── 3. CDN Cache Purge Log ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cdn_cache_purge_log (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    zone_id         BIGINT NOT NULL REFERENCES cdn_zones(id) ON DELETE CASCADE,
    purge_type      VARCHAR(16) NOT NULL
                    CHECK (purge_type IN ('all','url','tag','host','prefix')),
    targets         TEXT[] DEFAULT ARRAY[]::TEXT[],          -- URLs / tags / hosts purged
    purged_by       VARCHAR(255),                             -- email of actor
    purged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','success','partial','failed')),
    provider_job_id VARCHAR(120),                             -- CF/Fastly job id
    error_message   TEXT,
    duration_ms     INT
);
CREATE INDEX IF NOT EXISTS idx_cdn_purge_zone       ON cdn_cache_purge_log(zone_id, purged_at DESC);
CREATE INDEX IF NOT EXISTS idx_cdn_purge_ws         ON cdn_cache_purge_log(workspace_id, purged_at DESC);
CREATE INDEX IF NOT EXISTS idx_cdn_purge_status     ON cdn_cache_purge_log(status, purged_at DESC);


-- ─── 4. CDN Security Rules (WAF / Rate Limit / IP block / Country) ─────────
CREATE TABLE IF NOT EXISTS cdn_security_rules (
    id              BIGSERIAL PRIMARY KEY,
    zone_id         BIGINT NOT NULL REFERENCES cdn_zones(id) ON DELETE CASCADE,
    rule_type       VARCHAR(24) NOT NULL
                    CHECK (rule_type IN ('waf','rate_limit','ip_block','country_block','bot_protection','asn_block','user_agent_block')),
    rule_config     JSONB NOT NULL DEFAULT '{}'::JSONB,
    -- rule_config schemas per type:
    --   waf:            { "ruleset": "owasp", "paranoia_level": 1, "exclusions": [...] }
    --   rate_limit:     { "requests_per_minute": 60, "match_path": "/api/*", "match_method": "POST" }
    --   ip_block:       { "ips": ["1.2.3.4", "10.0.0.0/8"] }
    --   country_block:  { "country_codes": ["RU","KP"] }
    --   bot_protection: { "level": "high", "challenge": "captcha" }
    --   asn_block:      { "asns": ["AS12345"] }
    --   user_agent_block: { "patterns": ["BadBot/.*"] }
    action          VARCHAR(16) NOT NULL DEFAULT 'block'
                    CHECK (action IN ('block','challenge','log','allow','rate_limit')),
    description     TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    priority        INT NOT NULL DEFAULT 100,
    provider_rule_id VARCHAR(120),                            -- CF rule id sau khi sync
    hits_count      BIGINT NOT NULL DEFAULT 0,                -- số lần rule trigger (sync periodically)
    last_hit_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cdn_sec_zone         ON cdn_security_rules(zone_id, priority);
CREATE INDEX IF NOT EXISTS idx_cdn_sec_type         ON cdn_security_rules(zone_id, rule_type);
CREATE INDEX IF NOT EXISTS idx_cdn_sec_enabled      ON cdn_security_rules(zone_id, enabled);


-- ─── 5. CDN Analytics Daily ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cdn_analytics_daily (
    zone_id           BIGINT NOT NULL REFERENCES cdn_zones(id) ON DELETE CASCADE,
    date              DATE NOT NULL,
    requests          BIGINT NOT NULL DEFAULT 0,
    bandwidth_gb      NUMERIC(14, 4) NOT NULL DEFAULT 0,
    cache_hit_rate    NUMERIC(5, 2) NOT NULL DEFAULT 0,        -- 0.00 - 100.00 %
    threats_blocked   INT NOT NULL DEFAULT 0,
    unique_visitors   INT NOT NULL DEFAULT 0,
    requests_2xx      BIGINT NOT NULL DEFAULT 0,
    requests_3xx      BIGINT NOT NULL DEFAULT 0,
    requests_4xx      BIGINT NOT NULL DEFAULT 0,
    requests_5xx      BIGINT NOT NULL DEFAULT 0,
    avg_response_ms   INT NOT NULL DEFAULT 0,
    p95_response_ms   INT NOT NULL DEFAULT 0,
    bytes_saved_gb    NUMERIC(14, 4) NOT NULL DEFAULT 0,        -- bandwidth saved by cache
    top_countries     JSONB DEFAULT '[]'::JSONB,                -- [{"country":"VN","requests":12345}]
    top_paths         JSONB DEFAULT '[]'::JSONB,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (zone_id, date)
);
CREATE INDEX IF NOT EXISTS idx_cdn_analytics_date   ON cdn_analytics_daily(date DESC);
CREATE INDEX IF NOT EXISTS idx_cdn_analytics_zone   ON cdn_analytics_daily(zone_id, date DESC);


-- ─── 6. CDN Certificates (SSL/TLS lifecycle) ────────────────────────────────
CREATE TABLE IF NOT EXISTS cdn_certificates (
    id                  BIGSERIAL PRIMARY KEY,
    workspace_id        VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    zone_id             BIGINT REFERENCES cdn_zones(id) ON DELETE SET NULL,
    domain              VARCHAR(253) NOT NULL,
    san_domains         TEXT[] DEFAULT ARRAY[]::TEXT[],         -- Subject Alternative Names (wildcard etc.)
    cert_type           VARCHAR(24) NOT NULL DEFAULT 'lets_encrypt'
                        CHECK (cert_type IN ('lets_encrypt','custom','cloudflare_universal','google_managed','self_signed')),
    cert_pem            TEXT,                                    -- public cert (PEM, an toàn lưu DB)
    key_pem_secret_ref  VARCHAR(255),                            -- ref: gcp-secret://projects/.../secrets/foo
    chain_pem           TEXT,                                    -- intermediate chain
    fingerprint_sha256  VARCHAR(80),
    issuer              VARCHAR(255),
    issued_at           TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','issued','active','expiring','expired','revoked','error')),
    auto_renew          BOOLEAN NOT NULL DEFAULT TRUE,
    renew_attempts      INT NOT NULL DEFAULT 0,
    last_renew_at       TIMESTAMPTZ,
    last_error          TEXT,
    acme_challenge      VARCHAR(32) DEFAULT 'http-01'
                        CHECK (acme_challenge IN ('http-01','dns-01','tls-alpn-01')),
    provider_cert_id    VARCHAR(120),                            -- provider-side id
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cdn_cert_ws          ON cdn_certificates(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cdn_cert_domain      ON cdn_certificates(domain);
CREATE INDEX IF NOT EXISTS idx_cdn_cert_zone        ON cdn_certificates(zone_id) WHERE zone_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cdn_cert_expiring    ON cdn_certificates(expires_at) WHERE status IN ('active','issued','expiring');
CREATE INDEX IF NOT EXISTS idx_cdn_cert_renew       ON cdn_certificates(auto_renew, expires_at) WHERE auto_renew = TRUE;


-- ─── Convenience trigger: keep updated_at fresh ────────────────────────────
CREATE OR REPLACE FUNCTION cdn_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cdn_zones_touch       ON cdn_zones;
DROP TRIGGER IF EXISTS trg_cdn_routes_touch      ON cdn_routes;
DROP TRIGGER IF EXISTS trg_cdn_security_touch    ON cdn_security_rules;
DROP TRIGGER IF EXISTS trg_cdn_certs_touch       ON cdn_certificates;

CREATE TRIGGER trg_cdn_zones_touch
    BEFORE UPDATE ON cdn_zones
    FOR EACH ROW EXECUTE FUNCTION cdn_touch_updated_at();
CREATE TRIGGER trg_cdn_routes_touch
    BEFORE UPDATE ON cdn_routes
    FOR EACH ROW EXECUTE FUNCTION cdn_touch_updated_at();
CREATE TRIGGER trg_cdn_security_touch
    BEFORE UPDATE ON cdn_security_rules
    FOR EACH ROW EXECUTE FUNCTION cdn_touch_updated_at();
CREATE TRIGGER trg_cdn_certs_touch
    BEFORE UPDATE ON cdn_certificates
    FOR EACH ROW EXECUTE FUNCTION cdn_touch_updated_at();


-- ─── Seed: helper view for monthly aggregates (optional) ───────────────────
CREATE OR REPLACE VIEW cdn_analytics_monthly AS
    SELECT
        zone_id,
        date_trunc('month', date)::date AS month,
        SUM(requests)         AS requests,
        SUM(bandwidth_gb)     AS bandwidth_gb,
        AVG(cache_hit_rate)   AS avg_cache_hit_rate,
        SUM(threats_blocked)  AS threats_blocked,
        AVG(unique_visitors)  AS avg_unique_visitors,
        SUM(bytes_saved_gb)   AS bytes_saved_gb
    FROM cdn_analytics_daily
    GROUP BY zone_id, date_trunc('month', date);

-- ============================================================================
-- End of migration 038_edge_cdn.sql
-- ============================================================================
