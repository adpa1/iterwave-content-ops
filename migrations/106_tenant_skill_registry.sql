-- Tenant-owned Skill manifests. Registry records are metadata only; this table never stores executable code.
CREATE TABLE IF NOT EXISTS tenant_skill_registry (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 120),
    version TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'custom',
    inputs_json TEXT NOT NULL DEFAULT '[]',
    outputs_json TEXT NOT NULL DEFAULT '[]',
    permissions_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    manifest_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenant_skill_enabled
    ON tenant_skill_registry(enabled, category, name);
