-- ============================================================
-- 020 — 人工审核驳回状态
--   guard_outcome 表示自动目标检查结果；人工审核驳回是独立维度。
--   默认不驳回，旧内容保持原行为。
-- ============================================================

ALTER TABLE contents ADD COLUMN review_rejected INTEGER NOT NULL DEFAULT 0
    CHECK (review_rejected IN (0, 1));
ALTER TABLE contents ADD COLUMN review_rejection_reason TEXT;
