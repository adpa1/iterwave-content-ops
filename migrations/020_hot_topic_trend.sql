-- ============================================================
-- 020 — 热点热度趋势（雷达「爆发 / 持续爆发 / 降温中 / 已降温」辅助判断）
--   heat_prev  上一轮采集时的热度，NULL = 只有一轮数据（不能凭空判趋势，R3）
--   heat_peak  在榜期间的热度峰值，用于判断「已降温」的回撤幅度
-- ============================================================

ALTER TABLE hot_topics ADD COLUMN heat_prev REAL;
ALTER TABLE hot_topics ADD COLUMN heat_peak REAL;

UPDATE hot_topics SET heat_peak = heat_score WHERE heat_peak IS NULL AND heat_score IS NOT NULL;
