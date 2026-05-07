-- ============================================================================
-- Migration 022 — Observability stack (metrics + traces + alerts)
-- Adds: app_metrics, app_traces, alert_rules, alert_events.
-- Powers Prometheus scrape endpoint, OpenTelemetry-compat tracing,
-- and config-driven alerting (per workspace).
-- ============================================================================

-- Application metrics (per workspace per minute aggregation)
CREATE TABLE IF NOT EXISTS app_metrics (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) REFERENCES workspaces(id) ON DELETE CASCADE,
    metric_name VARCHAR(80) NOT NULL,        -- 'http_request_total','router_complete_latency_ms','cache_hit_total'
    metric_type VARCHAR(20) NOT NULL,        -- 'counter','gauge','histogram'
    metric_value NUMERIC(20,4) NOT NULL,
    labels JSONB,                             -- {endpoint, method, status, model, tier...}
    bucket_minute TIMESTAMPTZ NOT NULL,       -- truncated to minute
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_metrics_ws_time ON app_metrics(workspace_id, bucket_minute DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON app_metrics(metric_name, bucket_minute DESC);

-- Distributed traces (OpenTelemetry-compatible)
CREATE TABLE IF NOT EXISTS app_traces (
    trace_id VARCHAR(40) NOT NULL,
    span_id VARCHAR(20) NOT NULL,
    parent_span_id VARCHAR(20),
    workspace_id VARCHAR(32) REFERENCES workspaces(id) ON DELETE CASCADE,
    operation_name VARCHAR(120) NOT NULL,
    service_name VARCHAR(60) DEFAULT 'zenicloud',
    started_at TIMESTAMPTZ NOT NULL,
    duration_ms INT NOT NULL,
    status VARCHAR(20) DEFAULT 'ok',          -- 'ok','error','timeout'
    attributes JSONB,
    error_message TEXT,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_traces_ws ON app_traces(workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_traces_op ON app_traces(operation_name, started_at DESC);

-- Alert rules (config-driven alerts)
CREATE TABLE IF NOT EXISTS alert_rules (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    metric_name VARCHAR(80) NOT NULL,
    condition VARCHAR(20) NOT NULL,            -- 'gt','lt','gte','lte','eq'
    threshold NUMERIC(20,4) NOT NULL,
    window_minutes INT NOT NULL DEFAULT 5,
    severity VARCHAR(20) DEFAULT 'warning',   -- 'info','warning','critical'
    enabled BOOLEAN DEFAULT TRUE,
    notify_channels TEXT[] DEFAULT ARRAY['email'],
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_rules_ws ON alert_rules(workspace_id, enabled);

-- Triggered alerts log
CREATE TABLE IF NOT EXISTS alert_events (
    id BIGSERIAL PRIMARY KEY,
    rule_id BIGINT REFERENCES alert_rules(id) ON DELETE CASCADE,
    workspace_id VARCHAR(32) REFERENCES workspaces(id) ON DELETE CASCADE,
    metric_value NUMERIC(20,4) NOT NULL,
    triggered_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    notified BOOLEAN DEFAULT FALSE,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_events_ws ON alert_events(workspace_id, triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_rule ON alert_events(rule_id, triggered_at DESC);
