-- Migration 056 — Zeni Realtime (Supabase Realtime parity)
-- WebSocket pub-sub channels backed by Cloud Pub/Sub
-- Use cases: live chat, presence, real-time dashboards, notifications, ClawWits microVM screen monitor

CREATE TABLE IF NOT EXISTS realtime_channels (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id VARCHAR(64) NOT NULL,
  name VARCHAR(120) NOT NULL,                 -- channel name eg: chat:room-1, presence:dashboard
  -- Access control
  visibility VARCHAR(20) DEFAULT 'private',   -- private | public | authenticated
  allowed_user_ids JSONB DEFAULT '[]'::jsonb, -- if visibility=private
  -- Settings
  max_subscribers INT DEFAULT 1000,
  message_retention_seconds INT DEFAULT 0,    -- 0 = no history (broadcast only)
  presence_enabled BOOLEAN DEFAULT FALSE,
  -- Metrics
  active_subscribers INT DEFAULT 0,
  total_messages_sent BIGINT DEFAULT 0,
  -- Audit
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  created_by UUID,
  UNIQUE(workspace_id, name)
);

CREATE INDEX IF NOT EXISTS idx_realtime_channels_ws ON realtime_channels(workspace_id);

CREATE TABLE IF NOT EXISTS realtime_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id UUID NOT NULL REFERENCES realtime_channels(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  -- Message
  event_type VARCHAR(60) NOT NULL,           -- "message" | "presence-join" | "presence-leave" | custom
  payload JSONB NOT NULL,
  sender_user_id VARCHAR(120),
  -- Audit
  published_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_realtime_msgs_channel ON realtime_messages(channel_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_realtime_msgs_ws ON realtime_messages(workspace_id, published_at DESC);

-- Active subscriptions (presence tracking)
CREATE TABLE IF NOT EXISTS realtime_subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id UUID NOT NULL REFERENCES realtime_channels(id) ON DELETE CASCADE,
  workspace_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(120),
  client_id VARCHAR(80),
  -- Connection
  ws_connection_id VARCHAR(120),
  presence_state JSONB DEFAULT '{}'::jsonb,
  -- Tracking
  subscribed_at TIMESTAMPTZ DEFAULT NOW(),
  last_heartbeat_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_realtime_subs_channel ON realtime_subscriptions(channel_id);
CREATE INDEX IF NOT EXISTS idx_realtime_subs_user ON realtime_subscriptions(workspace_id, user_id);

COMMENT ON TABLE realtime_channels IS 'Zeni Realtime: pub-sub channels (Supabase parity)';
COMMENT ON TABLE realtime_messages IS 'Published messages with optional retention';
COMMENT ON TABLE realtime_subscriptions IS 'Active subscriber tracking + presence state';
