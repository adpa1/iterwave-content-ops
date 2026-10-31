-- Soft-hide activation runs without deleting quota history, saved samples or audit events.
CREATE TABLE IF NOT EXISTS activation_run_deletions (
    sample_run_id TEXT PRIMARY KEY REFERENCES sample_runs(id),
    tenant_id TEXT NOT NULL,
    deleted_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activation_run_deletions_tenant_time
    ON activation_run_deletions(tenant_id, deleted_at DESC);
