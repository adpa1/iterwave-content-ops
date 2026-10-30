-- Keep deleted activation runs for quota accounting and audit; hide them from active history.
ALTER TABLE sample_runs ADD COLUMN deleted_at DATETIME;
CREATE INDEX IF NOT EXISTS idx_sample_runs_tenant_deleted_created
    ON sample_runs(tenant_id, deleted_at, created_at DESC);
