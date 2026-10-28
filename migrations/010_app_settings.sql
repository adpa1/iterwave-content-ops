-- ============================================================
-- 010 — 全局运营软暂停（R6 熔断之外的人工开关）
-- 说明：与 circuit_breaker_events 无关——熔断是平台级被动保护，
--       app_settings 的 ops_paused 是全局主动暂停（不写 ops_logs 的熔断记录）
-- ============================================================

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at DATETIME,
    updated_by TEXT
);
