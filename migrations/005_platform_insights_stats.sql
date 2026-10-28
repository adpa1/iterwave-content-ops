-- G-04 单平台纵深分析：platform_insights 补充统计列
-- R3：新增数值列一律可空（NULL = 未分析/无样本），禁止 DEFAULT 0
ALTER TABLE platform_insights ADD COLUMN sample_count INTEGER;
ALTER TABLE platform_insights ADD COLUMN avg_interaction_rate REAL;
ALTER TABLE platform_insights ADD COLUMN conclusion TEXT;
