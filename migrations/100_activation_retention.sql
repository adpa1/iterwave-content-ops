-- Activation and retention domain tables.
-- All records are stored in the active tenant database; tenant_id is retained for audit.
CREATE TABLE IF NOT EXISTS activation_cohort (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tenant_id TEXT NOT NULL,
    first_seen_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sample_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    account_description TEXT NOT NULL CHECK (length(account_description) BETWEEN 10 AND 300),
    status TEXT NOT NULL CHECK (status IN ('created','generating','quality_check','ready','failed','rejected')),
    model_mode TEXT NOT NULL DEFAULT 'byok' CHECK (model_mode IN ('byok','hosted_trial')),
    error_code TEXT,
    error_message_safe TEXT,
    idempotency_key TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    completed_at DATETIME,
    UNIQUE(tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_sample_runs_tenant_created ON sample_runs(tenant_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sample_runs_one_active_per_tenant
    ON sample_runs(tenant_id) WHERE status IN ('created','generating','quality_check');

CREATE TABLE IF NOT EXISTS sample_contents (
    id TEXT PRIMARY KEY,
    sample_run_id TEXT NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    angle TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    hashtags_json TEXT NOT NULL DEFAULT '[]',
    quality_status TEXT NOT NULL CHECK (quality_status IN ('pass','rejected')),
    quality_findings_json TEXT NOT NULL DEFAULT '[]',
    adopted_at DATETIME,
    rejected_at DATETIME,
    rejection_reason TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sample_contents_run ON sample_contents(sample_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sample_contents_tenant ON sample_contents(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS content_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    content_id TEXT,
    sample_content_id TEXT REFERENCES sample_contents(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'activation_started','sample_generation_started','sample_generation_succeeded',
        'sample_generation_failed','content_copied','content_saved','content_adopted','content_rejected',
        'publish_confirmed','note_url_added','metrics_submitted','weekly_report_viewed'
    )),
    event_at DATETIME NOT NULL,
    source TEXT NOT NULL DEFAULT 'web',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT,
    CHECK (content_id IS NOT NULL OR sample_content_id IS NOT NULL OR event_type IN ('activation_started','sample_generation_started','sample_generation_succeeded','sample_generation_failed','weekly_report_viewed'))
);
CREATE INDEX IF NOT EXISTS idx_content_events_tenant_time ON content_events(tenant_id, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_events_type_time ON content_events(event_type, event_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_content_events_idempotency
    ON content_events(tenant_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    content_id TEXT NOT NULL REFERENCES contents(id),
    platform TEXT NOT NULL CHECK (platform IN ('xhs','douyin','kuaishou','wechat_channels')),
    source TEXT NOT NULL CHECK (source IN ('manual','platform_readback','import')),
    views INTEGER CHECK (views IS NULL OR views >= 0),
    likes INTEGER CHECK (likes IS NULL OR likes >= 0),
    collects INTEGER CHECK (collects IS NULL OR collects >= 0),
    comments INTEGER CHECK (comments IS NULL OR comments >= 0),
    shares INTEGER CHECK (shares IS NULL OR shares >= 0),
    new_followers INTEGER CHECK (new_followers IS NULL OR new_followers >= 0),
    measured_at DATETIME NOT NULL,
    recorded_at DATETIME NOT NULL,
    correction_of TEXT REFERENCES metric_snapshots(id),
    created_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_content_time
    ON metric_snapshots(tenant_id, content_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_source_time
    ON metric_snapshots(tenant_id, source, measured_at DESC);
