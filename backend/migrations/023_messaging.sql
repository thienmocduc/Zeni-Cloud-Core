-- ============================================================================
-- Migration 023 — Messaging stack: Pub/Sub topics + subscribers
--                  + Cloud Tasks scheduled jobs + cross-cutting Dead-Letter Queue
-- Stream A handles 022 (observability). THIS = 023 (messaging).
-- ============================================================================

-- ─── Pub/Sub topics ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pubsub_topics (
    id              BIGSERIAL    PRIMARY KEY,
    workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(120) NOT NULL,
    description     TEXT,
    schema          JSONB,                              -- optional message schema
    retention_seconds INT        DEFAULT 604800,        -- 7 days default
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_pubsub_topics_ws
    ON pubsub_topics(workspace_id, created_at DESC);

-- ─── Subscriptions per topic ────────────────────────────────
CREATE TABLE IF NOT EXISTS pubsub_subscriptions (
    id              BIGSERIAL    PRIMARY KEY,
    topic_id        BIGINT       NOT NULL REFERENCES pubsub_topics(id) ON DELETE CASCADE,
    workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name            VARCHAR(120) NOT NULL,
    delivery_type   VARCHAR(20)  DEFAULT 'webhook'      -- 'webhook','pull','queue'
                                  CHECK (delivery_type IN ('webhook','pull','queue')),
    webhook_url     TEXT,
    webhook_secret  TEXT,                                -- HMAC signing key
    filter_expression TEXT,                              -- e.g. "payload.amount > 1000000"
    max_retry_count INT          DEFAULT 5,
    ack_deadline_seconds INT     DEFAULT 60,
    enabled         BOOLEAN      DEFAULT TRUE,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pubsub_subs_topic
    ON pubsub_subscriptions(topic_id) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_pubsub_subs_ws
    ON pubsub_subscriptions(workspace_id, created_at DESC);

-- ─── Messages published ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS pubsub_messages (
    id              BIGSERIAL    PRIMARY KEY,
    topic_id        BIGINT       NOT NULL REFERENCES pubsub_topics(id) ON DELETE CASCADE,
    workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    message_id      VARCHAR(40)  NOT NULL UNIQUE,       -- UUID
    payload         JSONB        NOT NULL,
    attributes      JSONB,
    published_at    TIMESTAMPTZ  DEFAULT NOW(),
    expires_at      TIMESTAMPTZ  NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pubsub_msg_topic
    ON pubsub_messages(topic_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_pubsub_msg_ws
    ON pubsub_messages(workspace_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_pubsub_msg_expires
    ON pubsub_messages(expires_at);

-- ─── Delivery attempts per subscription ─────────────────────
CREATE TABLE IF NOT EXISTS pubsub_deliveries (
    id              BIGSERIAL    PRIMARY KEY,
    message_id      VARCHAR(40)  NOT NULL,
    subscription_id BIGINT       NOT NULL REFERENCES pubsub_subscriptions(id) ON DELETE CASCADE,
    workspace_id    VARCHAR(32)  NOT NULL,
    attempt_count   INT          DEFAULT 0,
    status          VARCHAR(20)  DEFAULT 'pending'      -- 'pending','delivered','failed','dlq'
                                  CHECK (status IN ('pending','delivered','failed','dlq')),
    next_attempt_at TIMESTAMPTZ  DEFAULT NOW(),
    delivered_at    TIMESTAMPTZ,
    last_error      TEXT,
    response_code   INT,
    response_body   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pubsub_deliv_pending
    ON pubsub_deliveries(next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_pubsub_deliv_status
    ON pubsub_deliveries(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_pubsub_deliv_sub
    ON pubsub_deliveries(subscription_id, status);

-- ─── Cloud Tasks–style scheduled jobs ───────────────────────
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              BIGSERIAL    PRIMARY KEY,
    workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    task_name       VARCHAR(120) NOT NULL,
    target_url      TEXT         NOT NULL,
    method          VARCHAR(10)  DEFAULT 'POST'
                                  CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
    headers         JSONB,
    body            JSONB,
    scheduled_at    TIMESTAMPTZ  NOT NULL,
    status          VARCHAR(20)  DEFAULT 'pending'
                                  CHECK (status IN ('pending','succeeded','failed','cancelled','dlq')),
    executed_at     TIMESTAMPTZ,
    response_code   INT,
    response_body   TEXT,
    retry_count     INT          DEFAULT 0,
    max_retries     INT          DEFAULT 3,
    last_error      TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_pending
    ON scheduled_tasks(scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_ws
    ON scheduled_tasks(workspace_id, status, created_at DESC);

-- ─── Cross-cutting Dead-Letter Queue ────────────────────────
CREATE TABLE IF NOT EXISTS dlq_messages (
    id              BIGSERIAL    PRIMARY KEY,
    workspace_id    VARCHAR(32)  NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_type     VARCHAR(20)  NOT NULL              -- 'pubsub','task','webhook'
                                  CHECK (source_type IN ('pubsub','task','webhook')),
    source_id       BIGINT,                            -- ID in source table
    payload         JSONB        NOT NULL,
    failure_reason  TEXT,
    attempts        INT          DEFAULT 0,
    moved_to_dlq_at TIMESTAMPTZ  DEFAULT NOW(),
    requeued_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_dlq_ws
    ON dlq_messages(workspace_id, moved_to_dlq_at DESC);
CREATE INDEX IF NOT EXISTS idx_dlq_source
    ON dlq_messages(source_type, source_id);

-- ─── Grants for application user ────────────────────────────
GRANT ALL PRIVILEGES ON pubsub_topics            TO zeni_app;
GRANT ALL PRIVILEGES ON pubsub_subscriptions     TO zeni_app;
GRANT ALL PRIVILEGES ON pubsub_messages          TO zeni_app;
GRANT ALL PRIVILEGES ON pubsub_deliveries        TO zeni_app;
GRANT ALL PRIVILEGES ON scheduled_tasks          TO zeni_app;
GRANT ALL PRIVILEGES ON dlq_messages             TO zeni_app;
GRANT USAGE, SELECT ON pubsub_topics_id_seq          TO zeni_app;
GRANT USAGE, SELECT ON pubsub_subscriptions_id_seq   TO zeni_app;
GRANT USAGE, SELECT ON pubsub_messages_id_seq        TO zeni_app;
GRANT USAGE, SELECT ON pubsub_deliveries_id_seq      TO zeni_app;
GRANT USAGE, SELECT ON scheduled_tasks_id_seq        TO zeni_app;
GRANT USAGE, SELECT ON dlq_messages_id_seq           TO zeni_app;
