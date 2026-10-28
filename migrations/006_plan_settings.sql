-- ============================================================
-- 006 — 母计划可编辑化（G-09 总计划 tab，§19 / R18）
-- 1) plan_settings：单行母计划（用户在 UI 编辑后落库，重启不丢）
-- 2) spiral_rounds.guard_rejections：G2 拦截次数（生产线 tab 阶段计数）
--    该列是运维计数器（不是业务指标），仍保持可空，由执行器显式写入
-- ============================================================

CREATE TABLE IF NOT EXISTS plan_settings (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    plan_snapshot  TEXT NOT NULL,          -- JSON：topic_count_per_platform / dimension_allocation / enabled_platforms / content_format
    plan_signature TEXT NOT NULL,
    updated_at     DATETIME,
    updated_by     TEXT
);

ALTER TABLE spiral_rounds ADD COLUMN guard_rejections INTEGER;

CREATE INDEX IF NOT EXISTS idx_signature_history_created ON signature_history(created_at);
