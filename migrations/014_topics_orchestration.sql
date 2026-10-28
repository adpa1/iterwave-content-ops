-- ============================================================
-- 014 — 选题池管理 / 编排可配置 / 运行报告留存
--   topic_index.summary/link：手动录入与热点转入的选题也能带摘要和原文链接
--   topic_index.excluded_at：软删除（KB 文件里的选题也能从选题池剔除，可恢复）
--   orchestration_settings：Agent 编排（步骤开关 + 顺序），执行器按此运行
--   spiral_rounds.run_report：每轮执行报告（编排预览展示各步状态与耗时）
-- ============================================================

ALTER TABLE topic_index ADD COLUMN summary         TEXT;
ALTER TABLE topic_index ADD COLUMN link            TEXT;
ALTER TABLE topic_index ADD COLUMN excluded_at     DATETIME;
ALTER TABLE topic_index ADD COLUMN excluded_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_topic_index_excluded ON topic_index(excluded_at);

CREATE TABLE IF NOT EXISTS orchestration_settings (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    steps      TEXT NOT NULL,          -- JSON [{id, enabled}]，数组顺序即执行顺序
    updated_at DATETIME,
    updated_by TEXT
);

ALTER TABLE spiral_rounds ADD COLUMN run_report TEXT;
