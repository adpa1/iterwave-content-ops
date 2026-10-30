-- Audit trail for explicit operator confirmation when importing sensitive hot topics.
CREATE TABLE IF NOT EXISTS hot_topic_import_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id TEXT NOT NULL,
    title TEXT NOT NULL,
    categories TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hot_topic_import_audit_topic
    ON hot_topic_import_audit(topic_id, confirmed_at);
