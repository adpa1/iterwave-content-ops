-- 031 — SQLite-backed tenant runtime configuration.
-- Runtime configuration is tenant-scoped because each tenant owns a separate DB.
CREATE TABLE IF NOT EXISTS tenant_settings (
    key          TEXT PRIMARY KEY,
    value_json   TEXT NOT NULL,
    value_type   TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    updated_by   TEXT NOT NULL DEFAULT 'system'
);
CREATE TABLE IF NOT EXISTS tenant_setting_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version        INTEGER NOT NULL,
    snapshot_json  TEXT NOT NULL,
    changed_at     TEXT NOT NULL,
    changed_by     TEXT NOT NULL DEFAULT 'system',
    change_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tenant_setting_versions_version
    ON tenant_setting_versions(version DESC);
CREATE TABLE IF NOT EXISTS tenant_setting_migrations (
    migration_key TEXT PRIMARY KEY,
    completed_at  TEXT NOT NULL,
    imported_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0
);
