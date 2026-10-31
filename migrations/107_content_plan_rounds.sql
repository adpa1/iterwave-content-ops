-- Associate content plans with queued production rounds.
CREATE TABLE IF NOT EXISTS content_plan_rounds (
    plan_id TEXT NOT NULL REFERENCES content_plans(id) ON DELETE CASCADE,
    round_id INTEGER NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, round_id),
    UNIQUE(round_id)
);
CREATE INDEX IF NOT EXISTS idx_content_plan_rounds_plan
    ON content_plan_rounds(plan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_plan_rounds_task
    ON content_plan_rounds(task_id);
