-- Tenant-scoped editorial plans. Plans are planning records only and never publish.
CREATE TABLE IF NOT EXISTS content_plans (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 160),
    topic TEXT NOT NULL DEFAULT '',
    platforms_json TEXT NOT NULL DEFAULT '[]',
    planned_count INTEGER NOT NULL DEFAULT 1 CHECK(planned_count BETWEEN 1 AND 50),
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','generating','generated','reviewed','archived','cancelled','failed')),
    notes TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_plans_date
    ON content_plans(scheduled_date, status);
CREATE INDEX IF NOT EXISTS idx_content_plans_status
    ON content_plans(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS content_plan_drafts (
    plan_id TEXT NOT NULL REFERENCES content_plans(id) ON DELETE CASCADE,
    draft_id TEXT NOT NULL,
    round_id INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, draft_id)
);
CREATE INDEX IF NOT EXISTS idx_content_plan_drafts_draft
    ON content_plan_drafts(draft_id);
