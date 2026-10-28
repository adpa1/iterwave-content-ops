-- ============================================================
-- 012 — 热点去重与保留（避免定时采集把库塞满）
--   去重规则 1：同平台 + 归一化标题 → 同一行（id = hash(平台|归一化标题)），重复采集只更新
--              热度 / last_seen_at / seen_count，不新增行
--   去重规则 2：跨平台同一事件（归一化标题相同或相似度 ≥ 阈值）→ duplicate_of 指向主条目，
--              雷达只展示主条目，标签匹配也只对主条目做（省 LLM 调用）
--   保留：last_seen_at 早于 hot_topics.retention_days 删除，并受 hot_topics.max_rows 总量上限约束
--   discovered_at 语义改为「首次发现时间」，last_seen_at 为「最近一次仍在榜」
-- ============================================================

ALTER TABLE hot_topics ADD COLUMN platform      TEXT;
ALTER TABLE hot_topics ADD COLUMN title_norm    TEXT;
ALTER TABLE hot_topics ADD COLUMN last_seen_at  DATETIME;
ALTER TABLE hot_topics ADD COLUMN seen_count    INTEGER NOT NULL DEFAULT 1;
ALTER TABLE hot_topics ADD COLUMN duplicate_of  TEXT;

UPDATE hot_topics SET last_seen_at = discovered_at WHERE last_seen_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_hot_topics_last_seen ON hot_topics(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_hot_topics_dup       ON hot_topics(duplicate_of);
