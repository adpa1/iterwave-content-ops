-- ============================================================
-- 011 — 热点 × 账号标签匹配结果（热点雷达「与目标的关联」）
-- 说明：采集与匹配改为后台定时任务完成并落库，GET /api/hot_topics 只读库。
--   match_score    0–100，NULL = 尚未研判（不是 0 分）
--   match_method   keyword（关键词/维度命中，无 LLM 时） | llm（语义研判）
--   match_version  画像签名（定位关键词 + 禁止方向 + 维度 + prompt 版本），画像变化后自动重判
-- ============================================================

ALTER TABLE hot_topics ADD COLUMN match_score     REAL;
ALTER TABLE hot_topics ADD COLUMN match_dimension TEXT;
ALTER TABLE hot_topics ADD COLUMN match_angle     TEXT;
ALTER TABLE hot_topics ADD COLUMN match_reason    TEXT;
ALTER TABLE hot_topics ADD COLUMN match_method    TEXT;
ALTER TABLE hot_topics ADD COLUMN match_version   TEXT;
ALTER TABLE hot_topics ADD COLUMN matched_at      DATETIME;
ALTER TABLE hot_topics ADD COLUMN forbidden_hit   INTEGER;

CREATE INDEX IF NOT EXISTS idx_hot_topics_match ON hot_topics(match_version, match_score);
