-- ============================================================
-- 002 — 执行器审计字段（P0-1 RoundExecutor）
-- G2 终极目标检查结果必须落库留痕（R2 可追溯）
-- ============================================================

ALTER TABLE contents ADD COLUMN guard_outcome TEXT;
ALTER TABLE contents ADD COLUMN guard_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_contents_round ON contents(round_id);
CREATE INDEX IF NOT EXISTS idx_drafts_content ON drafts(content_id);
