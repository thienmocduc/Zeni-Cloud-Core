-- ─────────────────────────────────────────────────────────────────
-- Zeni Mail — Email Marketing module (Mailchimp replacement)
-- Migration 029
-- Tables: lists, subscribers, templates, campaigns, sends, clicks,
--         automations, enrollments
-- ─────────────────────────────────────────────────────────────────

-- Subscriber lists
CREATE TABLE IF NOT EXISTS mail_lists (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    description TEXT,
    double_optin BOOLEAN DEFAULT TRUE,
    confirmation_email_template TEXT,
    welcome_email_template TEXT,
    subscriber_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_mail_lists_ws ON mail_lists(workspace_id);

-- Subscribers
CREATE TABLE IF NOT EXISTS mail_subscribers (
    id BIGSERIAL PRIMARY KEY,
    list_id BIGINT NOT NULL REFERENCES mail_lists(id) ON DELETE CASCADE,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    first_name VARCHAR(120),
    last_name VARCHAR(120),
    custom_fields JSONB,
    tags TEXT[],
    status VARCHAR(20) DEFAULT 'pending',     -- 'pending','active','bounced','unsubscribed','complained'
    confirmation_token TEXT,
    confirmed_at TIMESTAMPTZ,
    unsubscribed_at TIMESTAMPTZ,
    bounce_count INT DEFAULT 0,
    last_engagement_at TIMESTAMPTZ,
    subscribed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (list_id, email)
);
CREATE INDEX IF NOT EXISTS idx_mail_sub_status ON mail_subscribers(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_mail_sub_list ON mail_subscribers(list_id);
CREATE INDEX IF NOT EXISTS idx_mail_sub_token ON mail_subscribers(confirmation_token);
CREATE INDEX IF NOT EXISTS idx_mail_sub_tags ON mail_subscribers USING GIN(tags);

-- Email templates
CREATE TABLE IF NOT EXISTS mail_templates (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    body_text TEXT,
    variables TEXT[],                          -- ['{{first_name}}','{{company}}']
    category VARCHAR(40),
    is_system BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace_id, name)
);
CREATE INDEX IF NOT EXISTS idx_mail_templates_ws ON mail_templates(workspace_id);

-- Campaigns (one-time blasts)
CREATE TABLE IF NOT EXISTS mail_campaigns (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    subject TEXT NOT NULL,
    from_email TEXT NOT NULL,
    from_name VARCHAR(120),
    reply_to TEXT,
    body_html TEXT NOT NULL,
    body_text TEXT,
    template_id BIGINT REFERENCES mail_templates(id),
    list_id BIGINT REFERENCES mail_lists(id),
    segment_filter JSONB,                      -- e.g. {"tags": ["vip"], "status": "active"}
    schedule_type VARCHAR(20) DEFAULT 'immediate', -- 'immediate','scheduled','recurring'
    scheduled_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'draft',        -- 'draft','scheduled','sending','sent','paused'
    total_recipients INT DEFAULT 0,
    sent_count INT DEFAULT 0,
    delivered_count INT DEFAULT 0,
    open_count INT DEFAULT 0,
    click_count INT DEFAULT 0,
    bounce_count INT DEFAULT 0,
    unsubscribe_count INT DEFAULT 0,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mail_campaigns_ws ON mail_campaigns(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_mail_campaigns_sched ON mail_campaigns(scheduled_at) WHERE status = 'scheduled';

-- Sent email tracking
CREATE TABLE IF NOT EXISTS mail_sends (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT REFERENCES mail_campaigns(id) ON DELETE CASCADE,
    automation_id BIGINT,                       -- forward ref to mail_automations
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    subscriber_id BIGINT REFERENCES mail_subscribers(id) ON DELETE SET NULL,
    to_email TEXT NOT NULL,
    subject TEXT,
    message_id VARCHAR(80) UNIQUE,             -- for tracking pixel + click links
    status VARCHAR(20) DEFAULT 'queued',       -- 'queued','sent','bounced','delivered','opened','clicked','complained','unsubscribed'
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    opened_at TIMESTAMPTZ,
    clicked_at TIMESTAMPTZ,
    bounce_reason TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_mail_sends_campaign ON mail_sends(campaign_id);
CREATE INDEX IF NOT EXISTS idx_mail_sends_msg ON mail_sends(message_id);
CREATE INDEX IF NOT EXISTS idx_mail_sends_status ON mail_sends(status) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_mail_sends_ws ON mail_sends(workspace_id);

-- Click tracking
CREATE TABLE IF NOT EXISTS mail_clicks (
    id BIGSERIAL PRIMARY KEY,
    send_id BIGINT NOT NULL REFERENCES mail_sends(id) ON DELETE CASCADE,
    workspace_id VARCHAR(32) NOT NULL,
    url TEXT NOT NULL,
    clicked_at TIMESTAMPTZ DEFAULT NOW(),
    user_agent TEXT,
    ip_address INET
);
CREATE INDEX IF NOT EXISTS idx_mail_clicks_send ON mail_clicks(send_id);
CREATE INDEX IF NOT EXISTS idx_mail_clicks_ws ON mail_clicks(workspace_id);

-- Automations (drip campaigns / triggers)
CREATE TABLE IF NOT EXISTS mail_automations (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    trigger_type VARCHAR(40),                  -- 'subscribe','tag_added','date','event','inactivity'
    trigger_config JSONB,
    list_id BIGINT REFERENCES mail_lists(id),
    steps JSONB NOT NULL,                       -- [{wait_hours:0, template_id:1, condition:null}, ...]
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mail_auto_ws ON mail_automations(workspace_id, is_active);

-- Automation enrollments (subscriber currently in automation)
CREATE TABLE IF NOT EXISTS mail_enrollments (
    id BIGSERIAL PRIMARY KEY,
    automation_id BIGINT NOT NULL REFERENCES mail_automations(id) ON DELETE CASCADE,
    subscriber_id BIGINT NOT NULL REFERENCES mail_subscribers(id) ON DELETE CASCADE,
    workspace_id VARCHAR(32) NOT NULL,
    current_step INT DEFAULT 0,
    next_step_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'active',       -- 'active','paused','completed','exited'
    enrolled_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (automation_id, subscriber_id)
);
CREATE INDEX IF NOT EXISTS idx_enrollments_pending ON mail_enrollments(next_step_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_enrollments_ws ON mail_enrollments(workspace_id);

-- ─── Trigger: auto-update mail_lists.subscriber_count ───────────────
CREATE OR REPLACE FUNCTION mail_update_subscriber_count() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'active' THEN
            UPDATE mail_lists SET subscriber_count = subscriber_count + 1 WHERE id = NEW.list_id;
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.status = 'active' AND NEW.status != 'active' THEN
            UPDATE mail_lists SET subscriber_count = GREATEST(subscriber_count - 1, 0) WHERE id = NEW.list_id;
        ELSIF OLD.status != 'active' AND NEW.status = 'active' THEN
            UPDATE mail_lists SET subscriber_count = subscriber_count + 1 WHERE id = NEW.list_id;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.status = 'active' THEN
            UPDATE mail_lists SET subscriber_count = GREATEST(subscriber_count - 1, 0) WHERE id = OLD.list_id;
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_mail_subs_count ON mail_subscribers;
CREATE TRIGGER trg_mail_subs_count
    AFTER INSERT OR UPDATE OR DELETE ON mail_subscribers
    FOR EACH ROW EXECUTE FUNCTION mail_update_subscriber_count();

COMMENT ON TABLE mail_lists IS 'Zeni Mail — subscriber lists per workspace';
COMMENT ON TABLE mail_subscribers IS 'Zeni Mail — list subscribers with double opt-in';
COMMENT ON TABLE mail_templates IS 'Zeni Mail — reusable email templates with variables';
COMMENT ON TABLE mail_campaigns IS 'Zeni Mail — one-time email blasts (Mailchimp-style)';
COMMENT ON TABLE mail_sends IS 'Zeni Mail — per-recipient send tracking with engagement events';
COMMENT ON TABLE mail_clicks IS 'Zeni Mail — link click events';
COMMENT ON TABLE mail_automations IS 'Zeni Mail — drip / trigger automation flows';
COMMENT ON TABLE mail_enrollments IS 'Zeni Mail — subscribers currently inside an automation';
