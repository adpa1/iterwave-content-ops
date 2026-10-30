-- Persist weekly recommendation decisions and post-adoption outcomes.
CREATE TABLE IF NOT EXISTS weekly_action_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    suggestion_key TEXT NOT NULL,
    report_start DATE NOT NULL,
    platform TEXT NOT NULL,
    action TEXT NOT NULL,
    basis TEXT NOT NULL,
    verification TEXT NOT NULL,
    evidence_content_ids_json TEXT NOT NULL DEFAULT '[]',
    metric_snapshot_ids_json TEXT NOT NULL DEFAULT '[]',
    sample_size INTEGER NOT NULL DEFAULT 0 CHECK (sample_size >= 0),
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','adopted','ignored')),
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending','success','no_effect','unclear')),
    outcome_note TEXT,
    decided_at DATETIME,
    outcome_recorded_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (tenant_id, suggestion_key)
);
CREATE INDEX IF NOT EXISTS idx_weekly_action_feedback_tenant_period
    ON weekly_action_feedback(tenant_id, report_start DESC, decision);
